import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cove_book_forge.errors import ForgeErrorCode, ForgeException

_SCHEMA_VERSION = 1

_SCHEMA_V1 = (
    """
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
    )
    """,
    """
    CREATE TABLE chapters (
        book_id TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
        chapter_index INTEGER NOT NULL CHECK (chapter_index >= 0),
        title TEXT NOT NULL DEFAULT '',
        content TEXT NOT NULL,
        source_locator TEXT NOT NULL DEFAULT '',
        UNIQUE (book_id, chapter_index)
    )
    """,
    """
    CREATE TABLE external_sources (
        external_source_id INTEGER PRIMARY KEY,
        book_id TEXT NOT NULL REFERENCES books(book_id) ON DELETE CASCADE,
        source_system TEXT NOT NULL,
        external_book_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (source_system, external_book_id)
    )
    """,
    """
    CREATE TABLE chapter_snapshots (
        chapter_snapshot_id INTEGER PRIMARY KEY,
        external_source_id INTEGER NOT NULL
            REFERENCES external_sources(external_source_id) ON DELETE CASCADE,
        chapter_index INTEGER NOT NULL CHECK (chapter_index >= 0),
        snapshot_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (external_source_id, chapter_index)
    )
    """,
)


def _safe_database_error(exc: BaseException) -> ForgeException:
    return ForgeException(
        ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
        "Library storage is unavailable.",
        cause=exc,
    )


class LibraryDatabase:
    """Own SQLite connections, schema migration, and transaction boundaries."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        """Create and migrate the database in one explicit transaction."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.transaction() as connection:
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version > _SCHEMA_VERSION:
                    raise ForgeException(
                        ForgeErrorCode.CONFIG_INVALID,
                        "Library schema is newer than this application.",
                    )
                if version == 0:
                    for statement in _SCHEMA_V1:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except ForgeException:
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise _safe_database_error(exc) from exc

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a configured connection; callers own no setup details."""
        try:
            connection = sqlite3.connect(self.path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise _safe_database_error(exc) from exc
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit the unit of work or roll it back on every exception."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
