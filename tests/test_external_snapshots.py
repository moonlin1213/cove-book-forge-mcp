import hashlib
import json
import sqlite3
from pathlib import Path

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


def test_v1_snapshot_schema_migrates_forward_and_backfills_fingerprints(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    legacy_json = '{"z":"界","a":1}'
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE external_sources (
                external_source_id INTEGER PRIMARY KEY,
                book_id TEXT NOT NULL,
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
        )
        connection.execute(
            """
            INSERT INTO external_sources (
                external_source_id, book_id, source_system, external_book_id,
                created_at, updated_at
            ) VALUES (1, 'book-1', 'reader', 'external-1', 'created', 'updated')
            """
        )
        connection.execute(
            """
            INSERT INTO chapter_snapshots (
                external_source_id, chapter_index, snapshot_json, created_at, updated_at
            ) VALUES (1, 0, ?, 'created', 'updated')
            """,
            (legacy_json,),
        )

    database = LibraryDatabase(path)
    database.initialize()
    database.initialize()

    canonical = '{"a":1,"z":"界"}'
    with database.connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1]: row[3]
            for row in connection.execute("PRAGMA table_info(chapter_snapshots)").fetchall()
        }
        row = connection.execute(
            "SELECT snapshot_json, content_fingerprint FROM chapter_snapshots"
        ).fetchone()
    assert version == 1
    assert columns["content_fingerprint"] == 1
    assert row is not None
    assert row["snapshot_json"] == canonical
    assert row["content_fingerprint"] == hashlib.sha256(canonical.encode()).hexdigest()
