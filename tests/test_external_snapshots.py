import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from cove_book_forge.config import AppConfig
from cove_book_forge.contracts import (
    Annotation,
    BookFormat,
    BookMetadata,
    BookRef,
    ChapterContent,
    ChapterSnapshot,
    ExternalIdentity,
    ExtractedBook,
    Highlight,
    ImportMode,
    Reflection,
    UserNote,
)
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors import BookExtractorRegistry
from cove_book_forge.library import BookLibrary, LibraryDatabase, LibraryRepository

_V1_SCHEMA = """
CREATE TABLE books (
    book_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    total_chapters INTEGER NOT NULL DEFAULT 0 CHECK (total_chapters >= 0),
    format TEXT,
    import_mode TEXT,
    source_fingerprint TEXT,
    managed_source_path TEXT,
    reference_source_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (format, source_fingerprint)
);
CREATE TABLE chapters (
    book_id TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
    chapter_index INTEGER NOT NULL CHECK (chapter_index >= 0),
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source_locator TEXT NOT NULL DEFAULT '',
    UNIQUE (book_id, chapter_index)
);
CREATE TABLE external_sources (
    external_source_id INTEGER PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
    source_system TEXT NOT NULL,
    external_book_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_system, external_book_id)
);
CREATE TABLE chapter_snapshots (
    chapter_snapshot_id INTEGER PRIMARY KEY,
    external_source_id INTEGER NOT NULL
        REFERENCES external_sources(external_source_id) ON DELETE CASCADE,
    chapter_index INTEGER NOT NULL CHECK (chapter_index >= 0),
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (external_source_id, chapter_index)
);
PRAGMA user_version = 1;
"""

_LEGACY_BOOK_ID = "legacy-internal-book"
_LEGACY_EXTERNAL_SOURCE_ID = 17
_LEGACY_SNAPSHOT_ID = 23
_LEGACY_MANAGED_FINGERPRINT = "b" * 64
_LEGACY_MANAGED_PATH = "/private/legacy-managed-secret.pdf"
_V1_TABLES = ("books", "chapters", "external_sources", "chapter_snapshots")


class SyntheticPdfExtractor:
    def extract(self, source: Path, fingerprint: str) -> ExtractedBook:
        return ExtractedBook(
            format=BookFormat.PDF,
            metadata=BookMetadata(title="Managed fixture", total_chapters=1),
            chapters=(
                ChapterContent(
                    index=0,
                    title="Managed chapter",
                    content="Managed content.",
                    source_locator="pdf:pages:1-1",
                ),
            ),
            source_fingerprint=fingerprint,
        )


def _config(data_dir: Path, *, enabled: bool) -> AppConfig:
    return AppConfig.model_validate(
        {
            "library": {"enabled": enabled, "data_dir": data_dir},
            "model": {"provider": "test", "model": "test"},
        }
    )


def _library(
    data_dir: Path,
    *,
    enabled: bool = False,
) -> tuple[BookLibrary, LibraryDatabase]:
    database = LibraryDatabase(data_dir / "library.sqlite3")
    repository = LibraryRepository(database)
    library = BookLibrary(_config(data_dir, enabled=enabled), repository=repository)
    library.initialize()
    return library, database


def _snapshot(
    *,
    index: int = 0,
    total_chapters: int = 1,
    content: str = "External content.",
) -> ChapterSnapshot:
    return ChapterSnapshot(
        source_system="reader",
        external_book_id="external-1",
        book=BookMetadata(
            title="外部书籍",
            author="External Author",
            language="zh",
            total_chapters=total_chapters,
        ),
        chapter=ChapterContent(
            index=index,
            title=f"Chapter {index}",
            content=content,
            source_locator=f"reader:chapter:{index}",
        ),
        highlights=(Highlight(id="highlight-stable", text="External", paragraph_index=0),),
        user_notes=(UserNote(id="user-note-stable", text="Remember this", paragraph_index=0),),
        annotations=(
            Annotation(
                id="annotation-stable",
                text="Editorial context",
                author_label="Editor",
            ),
        ),
        reflections=(
            Reflection(
                id="reflection-stable",
                text="A lasting thought",
                author_label="Reader",
            ),
        ),
    )


def _create_v1_external_database(
    path: Path,
    snapshot_json: str,
    *,
    chapter_index: int = 2,
    managed_provenance: bool = False,
) -> None:
    provenance = (
        ("pdf", "reference", _LEGACY_MANAGED_FINGERPRINT, None, _LEGACY_MANAGED_PATH)
        if managed_provenance
        else (None, None, None, None, None)
    )
    chapter_title = "Managed chapter" if managed_provenance else "Untouched"
    chapter_content = (
        "Managed chapter remains byte-for-byte." if managed_provenance else "Untouched content."
    )
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(_V1_SCHEMA)
        connection.execute(
            """
            INSERT INTO books (
                book_id, title, author, language, total_chapters,
                format, import_mode, source_fingerprint,
                managed_source_path, reference_source_path,
                created_at, updated_at
            ) VALUES (?, 'Legacy title', 'Legacy author', 'zh', 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _LEGACY_BOOK_ID,
                *provenance,
                "2026-08-14T01:00:00+00:00",
                "2026-08-14T01:01:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO chapters (
                book_id, chapter_index, title, content, source_locator
            ) VALUES (?, 0, ?, ?, 'legacy:chapter:0')
            """,
            (_LEGACY_BOOK_ID, chapter_title, chapter_content),
        )
        connection.execute(
            """
            INSERT INTO external_sources (
                external_source_id, book_id, source_system, external_book_id,
                created_at, updated_at
            ) VALUES (?, ?, 'reader', 'external-1', ?, ?)
            """,
            (
                _LEGACY_EXTERNAL_SOURCE_ID,
                _LEGACY_BOOK_ID,
                "2026-08-14T02:00:00+00:00",
                "2026-08-14T02:01:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO chapter_snapshots (
                chapter_snapshot_id, external_source_id, chapter_index,
                snapshot_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _LEGACY_SNAPSHOT_ID,
                _LEGACY_EXTERNAL_SOURCE_ID,
                chapter_index,
                snapshot_json,
                "2026-08-14T03:00:00+00:00",
                "2026-08-14T03:01:00+00:00",
            ),
        )


def _read_v1_database_state(path: Path) -> tuple[object, ...]:
    file_bytes = path.read_bytes()
    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        schema = tuple(
            connection.execute(
                """
                SELECT name, sql
                FROM sqlite_master
                WHERE type = 'table' AND name IN (?, ?, ?, ?)
                ORDER BY name
                """,
                _V1_TABLES,
            ).fetchall()
        )
        rows = tuple(
            (
                table,
                tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()),
            )
            for table in _V1_TABLES
        )
    return file_bytes, version, schema, rows


def test_disabled_managed_library_still_accepts_external_books_and_snapshots(
    tmp_path: Path,
) -> None:
    library, _database = _library(tmp_path / "library", enabled=False)
    identity = ExternalIdentity(source_system="reader", external_book_id="external-1")

    book = library.upsert_external_book(
        identity,
        BookMetadata(title="External book", total_chapters=1),
    )
    snapshot_book = library.upsert_chapter_snapshot(_snapshot())

    assert snapshot_book == book
    assert library.get_book(book).source_available is True
    assert library.get_chapter(book, 0).content == "External content."
    assert library.list_books() == (library.get_book(book),)
    with pytest.raises(ForgeException) as exc_info:
        library.import_book(tmp_path / "missing.pdf")
    assert exc_info.value.code is ForgeErrorCode.CONFIG_INVALID


def test_external_identity_is_stable_and_metadata_total_never_shrinks(tmp_path: Path) -> None:
    library, _database = _library(tmp_path / "library")
    identity = ExternalIdentity(source_system="reader", external_book_id="external-1")

    first = library.upsert_external_book(
        identity,
        BookMetadata(title="First title", total_chapters=5),
    )
    second = library.upsert_external_book(
        identity,
        BookMetadata(title="Updated title", total_chapters=2),
    )
    other = library.upsert_external_book(
        ExternalIdentity(source_system="reader", external_book_id="external-2"),
        BookMetadata(title="Other book"),
    )

    stored = library.get_book(first)
    assert first == second
    assert other != first
    assert first.book_id not in {identity.external_book_id, identity.source_system}
    assert stored.metadata.title == "Updated title"
    assert stored.metadata.total_chapters == 5
    assert stored.format is None
    assert stored.import_mode is None
    assert len(stored.source_fingerprint) == 64
    assert stored.source_fingerprint == stored.source_fingerprint.lower()


def test_chapter_upsert_replaces_only_its_index_and_grows_total_metadata(
    tmp_path: Path,
) -> None:
    library, database = _library(tmp_path / "library")

    book = library.upsert_chapter_snapshot(_snapshot(index=0, total_chapters=1))
    library.upsert_chapter_snapshot(_snapshot(index=4, total_chapters=2, content="Fifth."))
    library.upsert_chapter_snapshot(_snapshot(index=0, total_chapters=1, content="Replacement."))

    assert library.get_chapter(book, 0).content == "Replacement."
    assert library.get_chapter(book, 4).content == "Fifth."
    assert library.get_book(book).metadata.total_chapters == 5
    with database.connect() as connection:
        snapshot_count = connection.execute("SELECT COUNT(*) FROM chapter_snapshots").fetchone()[0]
        chapter_count = connection.execute("SELECT COUNT(*) FROM chapters").fetchone()[0]
    assert snapshot_count == 2
    assert chapter_count == 2


def test_snapshot_json_is_canonical_fingerprinted_and_preserves_stable_ids(
    tmp_path: Path,
) -> None:
    library, database = _library(tmp_path / "library")
    snapshot = _snapshot()

    library.upsert_chapter_snapshot(snapshot)

    expected_json = json.dumps(
        snapshot.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    expected_fingerprint = hashlib.sha256(expected_json.encode("utf-8")).hexdigest()
    with database.connect() as connection:
        row = connection.execute(
            "SELECT snapshot_json, content_fingerprint FROM chapter_snapshots"
        ).fetchone()
    assert row is not None
    assert row["snapshot_json"] == expected_json
    assert row["content_fingerprint"] == expected_fingerprint
    payload = json.loads(row["snapshot_json"])
    assert payload["highlights"][0]["id"] == "highlight-stable"
    assert payload["user_notes"][0]["id"] == "user-note-stable"
    assert payload["annotations"][0]["id"] == "annotation-stable"
    assert payload["reflections"][0]["id"] == "reflection-stable"
    assert "moon_private" not in payload


def test_missing_external_chapter_is_distinct_from_unknown_book(tmp_path: Path) -> None:
    library, _database = _library(tmp_path / "library")
    book = library.upsert_external_book(
        ExternalIdentity(source_system="reader", external_book_id="external-1"),
        BookMetadata(title="External book", total_chapters=3),
    )

    with pytest.raises(ForgeException) as incomplete:
        library.get_chapter(book, 2)
    with pytest.raises(ForgeException) as unknown:
        library.get_chapter(BookRef(book_id="unknown-book"), 0)

    assert incomplete.value.code is ForgeErrorCode.EXTERNAL_BOOK_INCOMPLETE
    assert unknown.value.code is ForgeErrorCode.SOURCE_NOT_FOUND
    assert "external-1" not in str(incomplete.value)


def test_managed_and_external_books_coexist_without_identity_collisions(tmp_path: Path) -> None:
    data_dir = tmp_path / "library"
    registry = BookExtractorRegistry({BookFormat.PDF: SyntheticPdfExtractor()})
    library = BookLibrary(_config(data_dir, enabled=True), registry=registry)
    library.initialize()
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-1.7\nsynthetic")

    managed = library.import_book(source, ImportMode.REFERENCE)
    external = library.upsert_external_book(
        ExternalIdentity(
            source_system="reader",
            external_book_id=managed.source_fingerprint,
        ),
        BookMetadata(title="External book"),
    )

    assert external != managed.book
    assert {book.book for book in library.list_books()} == {managed.book, external}
    assert library.get_book(managed.book).format is BookFormat.PDF
    assert library.get_book(external).format is None


def test_real_v1_external_data_migrates_to_readable_idempotent_v3(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "library"
    path = data_dir / "library.sqlite3"
    snapshot = _snapshot(index=2, total_chapters=1, content="Migrated chapter.")
    legacy_json = json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2)
    _create_v1_external_database(path, legacy_json)
    library, database = _library(data_dir)
    book = BookRef(book_id=_LEGACY_BOOK_ID)

    canonical = json.dumps(
        snapshot.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    identity_json = json.dumps(
        snapshot.external_identity.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    )
    with database.connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        snapshot_row = connection.execute("SELECT * FROM chapter_snapshots").fetchone()
        external_row = connection.execute("SELECT * FROM external_sources").fetchone()
        book_row = connection.execute("SELECT * FROM books").fetchone()
        chapters = connection.execute(
            "SELECT chapter_index, content FROM chapters ORDER BY chapter_index"
        ).fetchall()
        foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
        migrated_state = tuple(
            tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1"))
            for table in ("books", "chapters", "external_sources", "chapter_snapshots")
        )
    assert version == 3
    assert snapshot_row is not None
    assert snapshot_row["chapter_snapshot_id"] == _LEGACY_SNAPSHOT_ID
    assert snapshot_row["external_source_id"] == _LEGACY_EXTERNAL_SOURCE_ID
    assert snapshot_row["created_at"] == "2026-08-14T03:00:00+00:00"
    assert snapshot_row["updated_at"] == "2026-08-14T03:01:00+00:00"
    assert snapshot_row["snapshot_json"] == canonical
    assert snapshot_row["content_fingerprint"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert external_row is not None
    assert external_row["external_source_id"] == _LEGACY_EXTERNAL_SOURCE_ID
    assert external_row["created_at"] == "2026-08-14T02:00:00+00:00"
    assert external_row["updated_at"] == "2026-08-14T02:01:00+00:00"
    assert book_row is not None
    assert book_row["source_fingerprint"] == hashlib.sha256(identity_json.encode()).hexdigest()
    assert book_row["total_chapters"] == 3
    assert book_row["created_at"] == "2026-08-14T01:00:00+00:00"
    assert book_row["updated_at"] == "2026-08-14T01:01:00+00:00"
    assert [(row["chapter_index"], row["content"]) for row in chapters] == [
        (0, "Untouched content."),
        (2, "Migrated chapter."),
    ]
    assert foreign_key_issues == []
    assert library.list_books() == (library.get_book(book),)
    assert library.get_book(book).source_available is True
    assert library.get_chapter(book, 0).content == "Untouched content."
    assert library.get_chapter(book, 2).content == "Migrated chapter."

    reopened, reopened_database = _library(data_dir)
    assert reopened.list_books() == library.list_books()
    assert reopened.get_chapter(book, 2) == library.get_chapter(book, 2)
    with reopened_database.connect() as connection:
        reopened_state = tuple(
            tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1"))
            for table in ("books", "chapters", "external_sources", "chapter_snapshots")
        )
    assert reopened_state == migrated_state

    with (
        pytest.raises(sqlite3.IntegrityError),
        reopened_database.transaction() as connection,
    ):
        connection.execute(
            """
            INSERT INTO chapter_snapshots (
                external_source_id, chapter_index, snapshot_json,
                content_fingerprint, created_at, updated_at
            ) VALUES (?, 2, '{}', ?, 'created', 'updated')
            """,
            (_LEGACY_EXTERNAL_SOURCE_ID, "0" * 64),
        )
    with (
        pytest.raises(sqlite3.IntegrityError),
        reopened_database.transaction() as connection,
    ):
        connection.execute(
            """
            INSERT INTO chapter_snapshots (
                external_source_id, chapter_index, snapshot_json,
                content_fingerprint, created_at, updated_at
            ) VALUES (?, -1, '{}', ?, 'created', 'updated')
            """,
            (_LEGACY_EXTERNAL_SOURCE_ID, "0" * 64),
        )
    with reopened_database.transaction() as connection:
        connection.execute("DELETE FROM books WHERE book_id = ?", (_LEGACY_BOOK_ID,))
    with reopened_database.connect() as connection:
        cascade_counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("books", "chapters", "external_sources", "chapter_snapshots")
        )
    assert cascade_counts == (0, 0, 0, 0)


@pytest.mark.parametrize("failure", ["malformed_json", "invalid_snapshot", "identity_mismatch"])
def test_invalid_v1_snapshot_migration_fails_closed_and_rolls_back(
    tmp_path: Path,
    failure: str,
) -> None:
    snapshot = _snapshot(index=2, total_chapters=1, content="Private chapter payload.")
    payload = snapshot.model_dump(mode="json")
    if failure == "malformed_json":
        raw_snapshot = '{"source_system":"reader","private":"unterminated"'
    elif failure == "invalid_snapshot":
        payload["moon_private"] = True
        raw_snapshot = json.dumps(payload, ensure_ascii=False)
    else:
        payload["external_book_id"] = "different-external-id"
        raw_snapshot = json.dumps(payload, ensure_ascii=False)
    path = tmp_path / failure / "library.sqlite3"
    _create_v1_external_database(path, raw_snapshot)

    with pytest.raises(ForgeException) as exc_info:
        LibraryDatabase(path).initialize()

    assert exc_info.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert "Private chapter payload" not in str(exc_info.value)
    assert "unterminated" not in str(exc_info.value)
    assert "SELECT" not in str(exc_info.value)
    assert str(path) not in str(exc_info.value)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chapter_snapshots)")}
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("books", "chapters", "external_sources", "chapter_snapshots")
        )
        stored_snapshot = connection.execute(
            "SELECT snapshot_json FROM chapter_snapshots"
        ).fetchone()[0]
        book_state = connection.execute(
            "SELECT source_fingerprint, total_chapters FROM books"
        ).fetchone()
        foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert "content_fingerprint" not in columns
    assert counts == (1, 1, 1, 1)
    assert stored_snapshot == raw_snapshot
    assert book_state == (None, 1)
    assert foreign_key_issues == []


@pytest.mark.parametrize(
    "snapshot_index",
    [
        pytest.param(0, id="same-managed-chapter-index"),
        pytest.param(2, id="different-managed-chapter-index"),
    ],
)
def test_v1_external_reference_to_managed_book_fails_closed_without_mutation(
    tmp_path: Path,
    snapshot_index: int,
) -> None:
    path = tmp_path / f"collision-{snapshot_index}" / "library.sqlite3"
    snapshot = _snapshot(
        index=snapshot_index,
        total_chapters=1,
        content=f"Collision snapshot secret at index {snapshot_index}.",
    )
    snapshot_json = json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False)
    _create_v1_external_database(
        path,
        snapshot_json,
        chapter_index=snapshot_index,
        managed_provenance=True,
    )
    original_state = _read_v1_database_state(path)

    with pytest.raises(ForgeException) as exc_info:
        LibraryDatabase(path).initialize()

    assert exc_info.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert exc_info.value.as_detail().details == {}
    public_error = str(exc_info.value)
    for private_value in (
        _LEGACY_MANAGED_PATH,
        _LEGACY_MANAGED_FINGERPRINT,
        snapshot.source_system,
        snapshot.external_book_id,
        snapshot_json,
        snapshot.chapter.content,
        "UPDATE books",
        "SQLite",
    ):
        assert private_value not in public_error
    assert _read_v1_database_state(path) == original_state

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        managed_provenance = connection.execute(
            """
            SELECT format, import_mode, source_fingerprint,
                   managed_source_path, reference_source_path,
                   created_at, updated_at
            FROM books
            """
        ).fetchone()
        chapters = connection.execute(
            "SELECT chapter_index, title, content, source_locator FROM chapters"
        ).fetchall()
        external_row = connection.execute("SELECT * FROM external_sources").fetchone()
        snapshot_row = connection.execute("SELECT * FROM chapter_snapshots").fetchone()
    assert managed_provenance == (
        "pdf",
        "reference",
        _LEGACY_MANAGED_FINGERPRINT,
        None,
        _LEGACY_MANAGED_PATH,
        "2026-08-14T01:00:00+00:00",
        "2026-08-14T01:01:00+00:00",
    )
    assert chapters == [
        (0, "Managed chapter", "Managed chapter remains byte-for-byte.", "legacy:chapter:0")
    ]
    assert external_row == (
        _LEGACY_EXTERNAL_SOURCE_ID,
        _LEGACY_BOOK_ID,
        "reader",
        "external-1",
        "2026-08-14T02:00:00+00:00",
        "2026-08-14T02:01:00+00:00",
    )
    assert snapshot_row == (
        _LEGACY_SNAPSHOT_ID,
        _LEGACY_EXTERNAL_SOURCE_ID,
        snapshot_index,
        snapshot_json,
        "2026-08-14T03:00:00+00:00",
        "2026-08-14T03:01:00+00:00",
    )


def test_independent_initialized_libraries_converge_on_one_external_identity(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "library"
    first, first_database = _library(data_dir)
    second, _second_database = _library(data_dir)
    identity = ExternalIdentity(source_system="reader", external_book_id="race-book")
    metadata = BookMetadata(title="Racing book", total_chapters=1)
    barrier = Barrier(2)

    def race(library: BookLibrary) -> BookRef:
        barrier.wait()
        return library.upsert_external_book(identity, metadata)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (executor.submit(race, first), executor.submit(race, second))
        books = tuple(future.result(timeout=10) for future in futures)

    assert books[0] == books[1]
    with first_database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM external_sources").fetchone()[0] == 1


def test_snapshot_operation_rolls_back_every_table_and_returns_a_safe_error(
    tmp_path: Path,
) -> None:
    library, database = _library(tmp_path / "library")
    library.upsert_external_book(
        ExternalIdentity(source_system="reader", external_book_id="external-1"),
        BookMetadata(title="Before failure", total_chapters=1),
    )
    with database.connect() as connection:
        before = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("books", "external_sources", "chapter_snapshots", "chapters")
        )
        connection.execute(
            """
            CREATE TRIGGER fail_chapter_mirror
            BEFORE INSERT ON chapters
            BEGIN
                SELECT RAISE(FAIL, 'private SQLite mirror failure at /private/library.sqlite3');
            END
            """
        )

    snapshot = _snapshot(content="Private chapter content must never leak.")
    canonical_snapshot = json.dumps(
        snapshot.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    with pytest.raises(ForgeException) as exc_info:
        library.upsert_chapter_snapshot(snapshot)

    assert exc_info.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert canonical_snapshot not in str(exc_info.value)
    assert snapshot.chapter.content not in str(exc_info.value)
    assert "INSERT" not in str(exc_info.value)
    assert "SQLite mirror failure" not in str(exc_info.value)
    assert "/private/library.sqlite3" not in str(exc_info.value)
    with database.connect() as connection:
        after = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("books", "external_sources", "chapter_snapshots", "chapters")
        )
    assert before == (1, 1, 0, 0)
    assert after == before
