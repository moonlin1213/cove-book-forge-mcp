import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from cove_book_forge.contracts import (
    BookFormat,
    BookMetadata,
    BookRef,
    ChapterAnalysis,
    ChapterContent,
    ChapterSnapshot,
    ExternalIdentity,
    ImportedBook,
    ImportMode,
)
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.library.database import LibraryDatabase


@dataclass(frozen=True, slots=True)
class LibraryBookRecord:
    imported: ImportedBook
    managed_source_path: str | None
    reference_source_path: Path | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedBook:
    record: LibraryBookRecord
    created: bool


@dataclass(frozen=True, slots=True)
class ExternalBookRecord:
    book: BookRef
    metadata: BookMetadata
    source_fingerprint: str
    created_at: datetime
    updated_at: datetime


LibraryRecord = LibraryBookRecord | ExternalBookRecord


def _storage_error(exc: BaseException) -> ForgeException:
    return ForgeException(
        ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
        "Library storage is unavailable.",
        cause=exc,
    )


def _not_found() -> ForgeException:
    return ForgeException(ForgeErrorCode.SOURCE_NOT_FOUND, "Stored source was not found.")


def _external_incomplete() -> ForgeException:
    return ForgeException(
        ForgeErrorCode.EXTERNAL_BOOK_INCOMPLETE,
        "External book snapshot is incomplete.",
    )


def _invalid_cache_key() -> ForgeException:
    return ForgeException(ForgeErrorCode.CONFIG_INVALID, "Chapter analysis cache key is invalid.")


def _invalid_cached_analysis(cause: BaseException) -> ForgeException:
    return ForgeException(
        ForgeErrorCode.MODEL_OUTPUT_INVALID,
        "Stored chapter analysis is invalid.",
        cause=cause,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _validate_cache_key(
    source_system: str,
    external_book_id: str,
    chapter_index: int,
    input_fingerprint: str,
) -> None:
    if (
        not isinstance(source_system, str)
        or not 1 <= len(source_system) <= 80
        or not isinstance(external_book_id, str)
        or not 1 <= len(external_book_id) <= 240
        or not isinstance(chapter_index, int)
        or isinstance(chapter_index, bool)
        or chapter_index < 0
        or not isinstance(input_fingerprint, str)
        or len(input_fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in input_fingerprint)
    ):
        raise _invalid_cache_key()


class LibraryRepository:
    """Own library SQL and conversion from rows to typed records."""

    def __init__(self, database: LibraryDatabase) -> None:
        self._database = database

    def bind_database_anchor(
        self,
        directory_fd: int,
        validate: Callable[[], None],
    ) -> None:
        self._database.bind_file_anchor(directory_fd, "library.sqlite3", validate)

    def initialize(self) -> None:
        self._database.initialize()

    def load_chapter_analysis(
        self,
        source_system: str,
        external_book_id: str,
        chapter_index: int,
        input_fingerprint: str,
    ) -> ChapterAnalysis | None:
        _validate_cache_key(
            source_system,
            external_book_id,
            chapter_index,
            input_fingerprint,
        )
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT input_fingerprint, analysis_json
                    FROM chapter_analyses
                    WHERE source_system = ?
                      AND external_book_id = ?
                      AND chapter_index = ?
                    """,
                    (source_system, external_book_id, chapter_index),
                ).fetchone()
            if row is None or row["input_fingerprint"] != input_fingerprint:
                return None
            try:
                return ChapterAnalysis.model_validate(json.loads(str(row["analysis_json"])))
            except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
                raise _invalid_cached_analysis(exc) from exc
        except ForgeException:
            raise
        except sqlite3.Error as exc:
            raise _storage_error(exc) from exc

    def store_chapter_analysis(
        self,
        source_system: str,
        external_book_id: str,
        chapter_index: int,
        input_fingerprint: str,
        analysis: ChapterAnalysis,
    ) -> None:
        _validate_cache_key(
            source_system,
            external_book_id,
            chapter_index,
            input_fingerprint,
        )
        try:
            analysis_json = _canonical_json(analysis.model_dump(mode="json"))
            now = datetime.now(UTC).isoformat()
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO chapter_analyses (
                        source_system, external_book_id, chapter_index,
                        input_fingerprint, analysis_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_system, external_book_id, chapter_index) DO UPDATE SET
                        input_fingerprint = excluded.input_fingerprint,
                        analysis_json = excluded.analysis_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        source_system,
                        external_book_id,
                        chapter_index,
                        input_fingerprint,
                        analysis_json,
                        now,
                        now,
                    ),
                )
        except ForgeException:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise _storage_error(exc) from exc

    def find_managed_book(
        self,
        book_format: BookFormat,
        source_fingerprint: str,
    ) -> LibraryBookRecord | None:
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM books
                    WHERE format = ? AND source_fingerprint = ?
                    """,
                    (book_format.value, source_fingerprint),
                ).fetchone()
            return None if row is None else self._map_managed_book(row)
        except ForgeException:
            raise
        except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
            raise _storage_error(exc) from exc

    def persist_book(
        self,
        record: LibraryBookRecord,
        chapters: Sequence[ChapterContent],
        *,
        publish: Callable[[], None] | None = None,
    ) -> PersistedBook:
        """Persist one normalized book and optionally publish its managed source."""
        imported = record.imported
        try:
            with self._database.transaction() as connection:
                existing = connection.execute(
                    """
                    SELECT * FROM books
                    WHERE format = ? AND source_fingerprint = ?
                    """,
                    (imported.format.value, imported.source_fingerprint),
                ).fetchone()
                if existing is not None:
                    return PersistedBook(
                        record=self._map_managed_book(existing),
                        created=False,
                    )

                metadata = imported.metadata
                connection.execute(
                    """
                    INSERT INTO books (
                        book_id, title, author, language, total_chapters,
                        format, import_mode, source_fingerprint,
                        managed_source_path, reference_source_path,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        imported.book.book_id,
                        metadata.title,
                        metadata.author,
                        metadata.language,
                        metadata.total_chapters,
                        imported.format.value,
                        imported.import_mode.value,
                        imported.source_fingerprint,
                        record.managed_source_path,
                        str(record.reference_source_path)
                        if record.reference_source_path is not None
                        else None,
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO chapters (
                        book_id, chapter_index, title, content, source_locator
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            imported.book.book_id,
                            chapter.index,
                            chapter.title,
                            chapter.content,
                            chapter.source_locator,
                        )
                        for chapter in sorted(chapters, key=lambda item: item.index)
                    ),
                )
                if publish is not None:
                    publish()
            return PersistedBook(record=record, created=True)
        except ForgeException:
            raise
        except sqlite3.Error as exc:
            raise _storage_error(exc) from exc

    def upsert_external_book(
        self,
        identity: ExternalIdentity,
        metadata: BookMetadata,
        *,
        candidate: BookRef,
        source_fingerprint: str,
    ) -> ExternalBookRecord:
        try:
            with self._database.transaction() as connection:
                _external_source_id, record = self._upsert_external_book(
                    connection,
                    identity,
                    metadata,
                    candidate=candidate,
                    source_fingerprint=source_fingerprint,
                    minimum_total=0,
                )
            return record
        except ForgeException:
            raise
        except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
            raise _storage_error(exc) from exc

    def upsert_chapter_snapshot(
        self,
        snapshot: ChapterSnapshot,
        *,
        candidate: BookRef,
        book_fingerprint: str,
        snapshot_json: str,
        content_fingerprint: str,
    ) -> ExternalBookRecord:
        try:
            with self._database.transaction() as connection:
                external_source_id, record = self._upsert_external_book(
                    connection,
                    snapshot.external_identity,
                    snapshot.book,
                    candidate=candidate,
                    source_fingerprint=book_fingerprint,
                    minimum_total=snapshot.chapter.index + 1,
                )
                now = record.updated_at.isoformat()
                connection.execute(
                    """
                    INSERT INTO chapter_snapshots (
                        external_source_id, chapter_index, snapshot_json,
                        content_fingerprint, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (external_source_id, chapter_index) DO UPDATE SET
                        snapshot_json = excluded.snapshot_json,
                        content_fingerprint = excluded.content_fingerprint,
                        updated_at = excluded.updated_at
                    """,
                    (
                        external_source_id,
                        snapshot.chapter.index,
                        snapshot_json,
                        content_fingerprint,
                        now,
                        now,
                    ),
                )
                chapter = snapshot.chapter
                connection.execute(
                    """
                    INSERT INTO chapters (
                        book_id, chapter_index, title, content, source_locator
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (book_id, chapter_index) DO UPDATE SET
                        title = excluded.title,
                        content = excluded.content,
                        source_locator = excluded.source_locator
                    """,
                    (
                        record.book.book_id,
                        chapter.index,
                        chapter.title,
                        chapter.content,
                        chapter.source_locator,
                    ),
                )
            return record
        except ForgeException:
            raise
        except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
            raise _storage_error(exc) from exc

    @staticmethod
    def _upsert_external_book(
        connection: sqlite3.Connection,
        identity: ExternalIdentity,
        metadata: BookMetadata,
        *,
        candidate: BookRef,
        source_fingerprint: str,
        minimum_total: int,
    ) -> tuple[int, ExternalBookRecord]:
        existing = connection.execute(
            """
            SELECT es.external_source_id, b.*
            FROM external_sources AS es
            JOIN books AS b ON b.book_id = es.book_id
            WHERE es.source_system = ? AND es.external_book_id = ?
            """,
            (identity.source_system, identity.external_book_id),
        ).fetchone()
        now = datetime.now(UTC)
        requested_total = max(metadata.total_chapters, minimum_total)
        if existing is not None:
            book = BookRef(book_id=str(existing["book_id"]))
            created_at = datetime.fromisoformat(str(existing["created_at"]))
            total_chapters = max(int(existing["total_chapters"]), requested_total)
            connection.execute(
                """
                UPDATE books
                SET title = ?, author = ?, language = ?, total_chapters = ?,
                    source_fingerprint = ?, updated_at = ?
                WHERE book_id = ?
                """,
                (
                    metadata.title,
                    metadata.author,
                    metadata.language,
                    total_chapters,
                    source_fingerprint,
                    now.isoformat(),
                    book.book_id,
                ),
            )
            external_source_id = int(existing["external_source_id"])
            connection.execute(
                "UPDATE external_sources SET updated_at = ? WHERE external_source_id = ?",
                (now.isoformat(), external_source_id),
            )
        else:
            book = candidate
            created_at = now
            total_chapters = requested_total
            connection.execute(
                """
                INSERT INTO books (
                    book_id, title, author, language, total_chapters,
                    source_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    book.book_id,
                    metadata.title,
                    metadata.author,
                    metadata.language,
                    total_chapters,
                    source_fingerprint,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            cursor = connection.execute(
                """
                INSERT INTO external_sources (
                    book_id, source_system, external_book_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    book.book_id,
                    identity.source_system,
                    identity.external_book_id,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            if cursor.lastrowid is None:
                raise sqlite3.DatabaseError("external source row id was not returned")
            external_source_id = cursor.lastrowid
        record = ExternalBookRecord(
            book=book,
            metadata=BookMetadata(
                title=metadata.title,
                author=metadata.author,
                language=metadata.language,
                total_chapters=total_chapters,
            ),
            source_fingerprint=source_fingerprint,
            created_at=created_at,
            updated_at=now,
        )
        return external_source_id, record

    def list_books(self) -> tuple[LibraryRecord, ...]:
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT b.*, EXISTS (
                        SELECT 1 FROM external_sources AS es WHERE es.book_id = b.book_id
                    ) AS is_external
                    FROM books AS b
                    ORDER BY b.created_at ASC, b.book_id ASC
                    """
                ).fetchall()
            return tuple(self._map_book(row) for row in rows)
        except ForgeException:
            raise
        except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
            raise _storage_error(exc) from exc

    def get_book(self, book: BookRef) -> LibraryRecord:
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT b.*, EXISTS (
                        SELECT 1 FROM external_sources AS es WHERE es.book_id = b.book_id
                    ) AS is_external
                    FROM books AS b
                    WHERE b.book_id = ?
                    """,
                    (book.book_id,),
                ).fetchone()
            if row is None:
                raise _not_found()
            return self._map_book(row)
        except ForgeException:
            raise
        except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
            raise _storage_error(exc) from exc

    def get_chapter(self, book: BookRef, index: int) -> ChapterContent:
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT chapter_index, title, content, source_locator
                    FROM chapters
                    WHERE book_id = ? AND chapter_index = ?
                    """,
                    (book.book_id, index),
                ).fetchone()
                if row is None:
                    external = connection.execute(
                        "SELECT 1 FROM external_sources WHERE book_id = ?",
                        (book.book_id,),
                    ).fetchone()
                    if external is not None:
                        raise _external_incomplete()
                    raise _not_found()
            return ChapterContent(
                index=int(row["chapter_index"]),
                title=str(row["title"]),
                content=str(row["content"]),
                source_locator=str(row["source_locator"]),
            )
        except ForgeException:
            raise
        except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
            raise _storage_error(exc) from exc

    @staticmethod
    def new_record(
        imported: ImportedBook,
        *,
        managed_source_path: str | None,
        reference_source_path: Path | None,
    ) -> LibraryBookRecord:
        now = datetime.now(UTC)
        return LibraryBookRecord(
            imported=imported,
            managed_source_path=managed_source_path,
            reference_source_path=reference_source_path,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _map_book(row: sqlite3.Row) -> LibraryRecord:
        if bool(row["is_external"]):
            return LibraryRepository._map_external_book(row)
        return LibraryRepository._map_managed_book(row)

    @staticmethod
    def _map_managed_book(row: sqlite3.Row) -> LibraryBookRecord:
        source_fingerprint = row["source_fingerprint"]
        book_format = row["format"]
        import_mode = row["import_mode"]
        if not all(
            isinstance(value, str) for value in (source_fingerprint, book_format, import_mode)
        ):
            raise ValueError("managed book provenance is incomplete")
        imported = ImportedBook(
            book=BookRef(book_id=str(row["book_id"])),
            metadata=BookMetadata(
                title=str(row["title"]),
                author=str(row["author"]),
                language=str(row["language"]),
                total_chapters=int(row["total_chapters"]),
            ),
            format=BookFormat(book_format),
            import_mode=ImportMode(import_mode),
            source_fingerprint=source_fingerprint,
        )
        managed_path = row["managed_source_path"]
        reference_path = row["reference_source_path"]
        return LibraryBookRecord(
            imported=imported,
            managed_source_path=str(managed_path) if managed_path is not None else None,
            reference_source_path=Path(str(reference_path)) if reference_path is not None else None,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _map_external_book(row: sqlite3.Row) -> ExternalBookRecord:
        source_fingerprint = row["source_fingerprint"]
        if (
            not isinstance(source_fingerprint, str)
            or row["format"] is not None
            or row["import_mode"] is not None
            or row["managed_source_path"] is not None
            or row["reference_source_path"] is not None
        ):
            raise ValueError("external book provenance is invalid")
        return ExternalBookRecord(
            book=BookRef(book_id=str(row["book_id"])),
            metadata=BookMetadata(
                title=str(row["title"]),
                author=str(row["author"]),
                language=str(row["language"]),
                total_chapters=int(row["total_chapters"]),
            ),
            source_fingerprint=source_fingerprint,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )
