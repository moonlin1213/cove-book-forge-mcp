import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from threading import RLock
from uuid import uuid4

from cove_book_forge.contracts import ChapterSnapshot, ExternalIdentity
from cove_book_forge.errors import ForgeErrorCode, ForgeException

_SCHEMA_VERSION = 2

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

_REQUIRED_V1_COLUMNS = {
    "books": frozenset(
        {
            "book_id",
            "title",
            "author",
            "language",
            "total_chapters",
            "format",
            "import_mode",
            "source_fingerprint",
            "managed_source_path",
            "reference_source_path",
            "created_at",
            "updated_at",
        }
    ),
    "chapters": frozenset({"book_id", "chapter_index", "title", "content", "source_locator"}),
    "external_sources": frozenset(
        {
            "external_source_id",
            "book_id",
            "source_system",
            "external_book_id",
            "created_at",
            "updated_at",
        }
    ),
    "chapter_snapshots": frozenset(
        {
            "chapter_snapshot_id",
            "external_source_id",
            "chapter_index",
            "snapshot_json",
            "created_at",
            "updated_at",
        }
    ),
}


class _LibrarySchemaReadiness(StrEnum):
    UNINITIALIZED = "uninitialized"
    MIGRATION_PENDING = "migration_pending"
    READY = "ready"


def _inspect_library_schema(connection: sqlite3.Connection) -> _LibrarySchemaReadiness:
    """Inspect the application schema through an already read-only connection."""
    quick_check = connection.execute("PRAGMA quick_check(1)").fetchone()
    if quick_check is None or quick_check[0] != "ok":
        raise sqlite3.DatabaseError("integrity check failed")

    version_row = connection.execute("PRAGMA user_version").fetchone()
    if version_row is None:
        raise sqlite3.DatabaseError("schema version unavailable")
    version = int(version_row[0])
    schema_objects = {
        str(row[0]): str(row[1])
        for row in connection.execute(
            """
            SELECT name, type
            FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    }
    if version == 0:
        if schema_objects:
            raise sqlite3.DatabaseError("unrecognized unversioned schema")
        return _LibrarySchemaReadiness.UNINITIALIZED
    if version not in {1, _SCHEMA_VERSION}:
        raise sqlite3.DatabaseError("unsupported schema version")

    required_columns = {
        table: columns
        | (
            {"content_fingerprint"}
            if version == _SCHEMA_VERSION and table == "chapter_snapshots"
            else set()
        )
        for table, columns in _REQUIRED_V1_COLUMNS.items()
    }
    if any(schema_objects.get(table) != "table" for table in required_columns):
        raise sqlite3.DatabaseError("required application table is missing")
    for table, expected_columns in required_columns.items():
        actual_columns = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM pragma_table_info(?)",
                (table,),
            ).fetchall()
        }
        if not expected_columns <= actual_columns:
            raise sqlite3.DatabaseError("required application column is missing")

    if version == 1:
        return _LibrarySchemaReadiness.MIGRATION_PENDING
    return _LibrarySchemaReadiness.READY


@dataclass(frozen=True, slots=True)
class _DatabaseFileAnchor:
    directory_fd: int
    filename: str
    validate: Callable[[], None]


@dataclass(frozen=True, slots=True)
class _ExternalMigration:
    external_source_id: int
    book_id: str
    identity: ExternalIdentity
    book_fingerprint: str


@dataclass(frozen=True, slots=True)
class _SnapshotMigration:
    chapter_snapshot_id: int
    external_source_id: int
    snapshot: ChapterSnapshot
    snapshot_json: str
    content_fingerprint: str
    created_at: str
    updated_at: str


def _safe_database_error(exc: BaseException) -> ForgeException:
    return ForgeException(
        ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
        "Library storage is unavailable.",
        cause=exc,
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _fingerprint(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LibraryDatabase:
    """Own SQLite connections, schema migration, and transaction boundaries."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: sqlite3.Connection | None = None
        self._file_anchor: _DatabaseFileAnchor | None = None
        self._connection_lock = RLock()

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
                    version = 1
                    connection.execute("PRAGMA user_version = 1")
                if version == 1:
                    self._migrate_snapshot_fingerprints(connection)
                    connection.execute("PRAGMA user_version = 2")
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
        external_migrations = LibraryDatabase._load_external_migrations(connection)
        snapshot_migrations = LibraryDatabase._load_snapshot_migrations(
            connection,
            external_migrations,
        )
        connection.execute("ALTER TABLE chapter_snapshots RENAME TO chapter_snapshots_v1")
        connection.execute(_SCHEMA_V2_CHAPTER_SNAPSHOTS)
        for external in external_migrations.values():
            cursor = connection.execute(
                "UPDATE books SET source_fingerprint = ? WHERE book_id = ?",
                (external.book_fingerprint, external.book_id),
            )
            if cursor.rowcount != 1:
                raise sqlite3.IntegrityError("external source book is missing")
        for migration in snapshot_migrations:
            connection.execute(
                """
                INSERT INTO chapter_snapshots (
                    chapter_snapshot_id, external_source_id, chapter_index,
                    snapshot_json, content_fingerprint, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    migration.chapter_snapshot_id,
                    migration.external_source_id,
                    migration.snapshot.chapter.index,
                    migration.snapshot_json,
                    migration.content_fingerprint,
                    migration.created_at,
                    migration.updated_at,
                ),
            )
            external = external_migrations[migration.external_source_id]
            chapter = migration.snapshot.chapter
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
                    external.book_id,
                    chapter.index,
                    chapter.title,
                    chapter.content,
                    chapter.source_locator,
                ),
            )
            connection.execute(
                """
                UPDATE books
                SET total_chapters = MAX(total_chapters, ?)
                WHERE book_id = ?
                """,
                (chapter.index + 1, external.book_id),
            )
        connection.execute("DROP TABLE chapter_snapshots_v1")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise sqlite3.IntegrityError("foreign key check failed after migration")

    @staticmethod
    def _load_external_migrations(
        connection: sqlite3.Connection,
    ) -> dict[int, _ExternalMigration]:
        rows = connection.execute(
            """
            SELECT
                es.*,
                b.book_id AS referenced_book_id,
                b.format AS book_format,
                b.import_mode AS book_import_mode,
                b.source_fingerprint AS book_source_fingerprint,
                b.managed_source_path AS book_managed_source_path,
                b.reference_source_path AS book_reference_source_path
            FROM external_sources AS es
            LEFT JOIN books AS b ON b.book_id = es.book_id
            ORDER BY es.external_source_id
            """
        ).fetchall()
        migrations: dict[int, _ExternalMigration] = {}
        seen_books: set[str] = set()
        for row in rows:
            if row["referenced_book_id"] is None:
                raise ValueError("external source book is missing")
            if any(
                row[field] is not None
                for field in (
                    "book_format",
                    "book_import_mode",
                    "book_managed_source_path",
                    "book_reference_source_path",
                )
            ):
                raise ValueError("external source references managed provenance")
            identity = ExternalIdentity(
                source_system=str(row["source_system"]),
                external_book_id=str(row["external_book_id"]),
            )
            book_id = str(row["book_id"])
            if book_id in seen_books:
                raise ValueError("external book has multiple identities")
            seen_books.add(book_id)
            canonical_identity = _canonical_json(identity.model_dump(mode="json"))
            external_source_id = int(row["external_source_id"])
            migrations[external_source_id] = _ExternalMigration(
                external_source_id=external_source_id,
                book_id=book_id,
                identity=identity,
                book_fingerprint=_fingerprint(canonical_identity),
            )
        return migrations

    @staticmethod
    def _load_snapshot_migrations(
        connection: sqlite3.Connection,
        external_migrations: dict[int, _ExternalMigration],
    ) -> tuple[_SnapshotMigration, ...]:
        rows = connection.execute(
            "SELECT * FROM chapter_snapshots ORDER BY chapter_snapshot_id"
        ).fetchall()
        migrations: list[_SnapshotMigration] = []
        for row in rows:
            external_source_id = int(row["external_source_id"])
            external = external_migrations.get(external_source_id)
            if external is None:
                raise ValueError("snapshot external source is missing")
            payload = json.loads(str(row["snapshot_json"]))
            snapshot = ChapterSnapshot.model_validate(payload)
            if snapshot.external_identity != external.identity or snapshot.chapter.index != int(
                row["chapter_index"]
            ):
                raise ValueError("snapshot identity or chapter index is inconsistent")
            canonical = _canonical_json(snapshot.model_dump(mode="json"))
            migrations.append(
                _SnapshotMigration(
                    chapter_snapshot_id=int(row["chapter_snapshot_id"]),
                    external_source_id=external_source_id,
                    snapshot=snapshot,
                    snapshot_json=canonical,
                    content_fingerprint=_fingerprint(canonical),
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )
            )
        return tuple(migrations)

    def _open_stable_connection(self) -> sqlite3.Connection:
        if self._file_anchor is not None:
            return self._open_anchored_connection(self._file_anchor)
        return self._open_path_connection(self.path)

    def _open_path_connection(self, path: Path) -> sqlite3.Connection:
        candidate: sqlite3.Connection | None = None
        try:
            candidate = sqlite3.connect(
                path,
                isolation_level=None,
                check_same_thread=False,
            )
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

            candidate = sqlite3.connect(
                temporary_directory / anchor_name,
                isolation_level=None,
                check_same_thread=False,
            )
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
        with self._connection_lock:
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
