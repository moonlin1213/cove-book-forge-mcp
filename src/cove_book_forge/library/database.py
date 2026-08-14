import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

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

_SCHEMA_V2_CHAPTER_SNAPSHOTS = """
CREATE TABLE chapter_snapshots (
    chapter_snapshot_id INTEGER PRIMARY KEY,
    external_source_id INTEGER NOT NULL
        REFERENCES external_sources(external_source_id) ON DELETE CASCADE,
    chapter_index INTEGER NOT NULL CHECK (chapter_index >= 0),
    snapshot_json TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (external_source_id, chapter_index)
)
"""


@dataclass(frozen=True, slots=True)
class _DatabaseFileAnchor:
    directory_fd: int
    filename: str
    validate: Callable[[], None]


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
        self._connection: sqlite3.Connection | None = None
        self._file_anchor: _DatabaseFileAnchor | None = None

    def bind_file_anchor(
        self,
        directory_fd: int,
        filename: str,
        validate: Callable[[], None],
    ) -> None:
        """Bind the first database open to an already-validated directory object."""
        if self._connection is not None:
            raise ForgeException(
                ForgeErrorCode.CONFIG_INVALID,
                "Library database is already initialized.",
            )
        self._file_anchor = _DatabaseFileAnchor(directory_fd, filename, validate)

    def initialize(self) -> None:
        """Create and migrate the database in one explicit transaction."""
        opened_here = self._connection is None
        try:
            if self._file_anchor is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            if opened_here:
                self._connection = self._open_stable_connection()
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
                    connection.execute("PRAGMA user_version = 1")
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(chapter_snapshots)").fetchall()
                }
                if "content_fingerprint" not in columns:
                    self._migrate_snapshot_fingerprints(connection)
        except ForgeException:
            if opened_here:
                self._close_connection()
            raise
        except (OSError, sqlite3.Error, ValueError) as exc:
            if opened_here:
                self._close_connection()
            raise _safe_database_error(exc) from exc

    @staticmethod
    def _migrate_snapshot_fingerprints(connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE chapter_snapshots RENAME TO chapter_snapshots_v1")
        connection.execute(_SCHEMA_V2_CHAPTER_SNAPSHOTS)
        rows = connection.execute(
            "SELECT * FROM chapter_snapshots_v1 ORDER BY chapter_snapshot_id"
        ).fetchall()
        for row in rows:
            payload = json.loads(str(row["snapshot_json"]))
            canonical = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO chapter_snapshots (
                    chapter_snapshot_id, external_source_id, chapter_index,
                    snapshot_json, content_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["chapter_snapshot_id"],
                    row["external_source_id"],
                    row["chapter_index"],
                    canonical,
                    fingerprint,
                    row["created_at"],
                    row["updated_at"],
                ),
            )
        connection.execute("DROP TABLE chapter_snapshots_v1")

    def _open_stable_connection(self) -> sqlite3.Connection:
        if self._file_anchor is not None:
            return self._open_anchored_connection(self._file_anchor)
        return self._open_path_connection(self.path)

    def _open_path_connection(self, path: Path) -> sqlite3.Connection:
        candidate: sqlite3.Connection | None = None
        try:
            candidate = sqlite3.connect(path, isolation_level=None)
            self._configure_connection(candidate)
            return candidate
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            if candidate is not None:
                with suppress(OSError, sqlite3.Error):
                    candidate.close()
            raise _safe_database_error(exc) from exc

    @staticmethod
    def _configure_connection(candidate: sqlite3.Connection) -> None:
        candidate.row_factory = sqlite3.Row
        candidate.execute("PRAGMA foreign_keys = ON")
        journal_mode = candidate.execute("PRAGMA journal_mode = MEMORY").fetchone()[0]
        if str(journal_mode).lower() != "memory":
            raise sqlite3.OperationalError("memory journal mode unavailable")
        candidate.execute("PRAGMA temp_store = MEMORY")

    def _open_anchored_connection(self, anchor: _DatabaseFileAnchor) -> sqlite3.Connection:
        database_fd: int | None = None
        anchor_fd: int | None = None
        candidate: sqlite3.Connection | None = None
        created_database = False
        database_identity: tuple[int, int] | None = None
        temporary_directory: Path | None = None
        anchor_name = "database.sqlite3"
        try:
            anchor.validate()
            flags = os.O_RDWR | os.O_NOFOLLOW
            try:
                database_fd = os.open(
                    anchor.filename,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=anchor.directory_fd,
                )
                created_database = True
            except FileExistsError:
                database_fd = os.open(anchor.filename, flags, dir_fd=anchor.directory_fd)
            status = os.fstat(database_fd)
            if not stat.S_ISREG(status.st_mode):
                raise OSError("anchored database is not regular")
            database_identity = status.st_dev, status.st_ino

            temporary_directory = Path(tempfile.mkdtemp(prefix="cove-library-db-"))
            anchor_fd = os.open(
                temporary_directory,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            os.link(
                anchor.filename,
                anchor_name,
                src_dir_fd=anchor.directory_fd,
                dst_dir_fd=anchor_fd,
                follow_symlinks=False,
            )
            linked_status = os.stat(anchor_name, dir_fd=anchor_fd, follow_symlinks=False)
            if (linked_status.st_dev, linked_status.st_ino) != database_identity:
                raise OSError("anchored database identity changed")

            candidate = sqlite3.connect(temporary_directory / anchor_name, isolation_level=None)
            self._configure_connection(candidate)
            anchor.validate()
            current_status = os.stat(
                anchor.filename,
                dir_fd=anchor.directory_fd,
                follow_symlinks=False,
            )
            if (current_status.st_dev, current_status.st_ino) != database_identity:
                raise OSError("anchored database identity changed")
            return candidate
        except ForgeException:
            if candidate is not None:
                with suppress(OSError, sqlite3.Error):
                    candidate.close()
            if created_database and database_identity is not None:
                self._remove_created_database(anchor, database_identity)
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            if candidate is not None:
                with suppress(OSError, sqlite3.Error):
                    candidate.close()
            if created_database and database_identity is not None:
                self._remove_created_database(anchor, database_identity)
            raise _safe_database_error(exc) from exc
        finally:
            if anchor_fd is not None:
                with suppress(OSError):
                    os.unlink(anchor_name, dir_fd=anchor_fd)
                with suppress(OSError):
                    os.close(anchor_fd)
            if temporary_directory is not None:
                with suppress(OSError):
                    temporary_directory.rmdir()
            if database_fd is not None:
                with suppress(OSError):
                    os.close(database_fd)

    @staticmethod
    def _remove_created_database(
        anchor: _DatabaseFileAnchor,
        identity: tuple[int, int],
    ) -> None:
        quarantine_name = f".library-cleanup-{uuid4().hex}"
        try:
            os.rename(
                anchor.filename,
                quarantine_name,
                src_dir_fd=anchor.directory_fd,
                dst_dir_fd=anchor.directory_fd,
            )
            status = os.stat(
                quarantine_name,
                dir_fd=anchor.directory_fd,
                follow_symlinks=False,
            )
            if (status.st_dev, status.st_ino) == identity:
                os.unlink(quarantine_name, dir_fd=anchor.directory_fd)
                return
            try:
                os.link(
                    quarantine_name,
                    anchor.filename,
                    src_dir_fd=anchor.directory_fd,
                    dst_dir_fd=anchor.directory_fd,
                    follow_symlinks=False,
                )
            except OSError:
                return
            os.unlink(quarantine_name, dir_fd=anchor.directory_fd)
        except OSError:
            return

    def _close_connection(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            with suppress(OSError, sqlite3.Error):
                connection.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield the connection anchored to the file opened during initialization."""
        connection = self._connection
        if connection is None:
            raise ForgeException(
                ForgeErrorCode.CONFIG_INVALID,
                "Library database must be initialized before use.",
            )
        yield connection

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
