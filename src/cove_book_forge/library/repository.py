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
    ChapterContent,
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


def _storage_error(exc: BaseException) -> ForgeException:
    return ForgeException(
        ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
        "Library storage is unavailable.",
        cause=exc,
    )


def _not_found() -> ForgeException:
    return ForgeException(ForgeErrorCode.SOURCE_NOT_FOUND, "Stored source was not found.")


class LibraryRepository:
    """Own managed-library SQL and conversion from rows to typed records."""

    def __init__(self, database: LibraryDatabase) -> None:
        self._database = database

    def initialize(self) -> None:
        self._database.initialize()

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
            return None if row is None else self._map_book(row)
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
                    return PersistedBook(record=self._map_book(existing), created=False)

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

    def list_books(self) -> tuple[LibraryBookRecord, ...]:
        try:
            with self._database.connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM books ORDER BY created_at ASC, book_id ASC"
                ).fetchall()
            return tuple(self._map_book(row) for row in rows)
        except ForgeException:
            raise
        except (sqlite3.Error, TypeError, ValueError, ValidationError) as exc:
            raise _storage_error(exc) from exc

    def get_book(self, book: BookRef) -> LibraryBookRecord:
        try:
            with self._database.connect() as connection:
                row = connection.execute(
                    "SELECT * FROM books WHERE book_id = ?",
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
    def _map_book(row: sqlite3.Row) -> LibraryBookRecord:
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
