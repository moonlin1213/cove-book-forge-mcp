import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

from cove_book_forge.config import AppConfig, library_data_path
from cove_book_forge.contracts import (
    BookMetadata,
    BookRef,
    ChapterContent,
    ChapterSnapshot,
    ExternalIdentity,
    ImportedBook,
    ImportMode,
    StoredBook,
)
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors import BookExtractorRegistry
from cove_book_forge.extractors.security import ExtractionLimits, fingerprint_source
from cove_book_forge.library.database import LibraryDatabase
from cove_book_forge.library.repository import (
    ExternalBookRecord,
    LibraryBookRecord,
    LibraryRecord,
    LibraryRepository,
    PersistedBook,
)

_HASH_CHUNK_SIZE = 1024 * 1024
_BinaryOpener = Callable[[Path, str], BinaryIO]
_FileIdentity = tuple[int, int]


@dataclass
class _ManagedImportAttempt:
    books_fd: int
    book_fd: int | None
    book_name: str
    stage_name: str
    final_name: str
    book_identity: _FileIdentity | None
    owned_book_dir: bool
    stage_identity: _FileIdentity | None = None
    final_identity: _FileIdentity | None = None
    book_fd_closed: bool = False


def _path_not_allowed(cause: BaseException | None = None) -> ForgeException:
    return ForgeException(
        ForgeErrorCode.PATH_NOT_ALLOWED,
        "Library path is not allowed.",
        cause=cause,
    )


def _storage_unavailable(cause: BaseException) -> ForgeException:
    return ForgeException(
        ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
        "Library storage is unavailable.",
        cause=cause,
    )


def _source_changed(cause: BaseException | None = None) -> ForgeException:
    return ForgeException(
        ForgeErrorCode.SOURCE_CHANGED,
        "Source changed while it was copied.",
        cause=cause,
    )


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise _storage_unavailable(exc) from exc


def _canonical_fingerprint(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _open_binary(path: Path, mode: str) -> BinaryIO:
    return cast(BinaryIO, path.open(mode))


def _validate_data_root(path: Path) -> Path:
    raw = str(path)
    if "\x00" in raw:
        raise _path_not_allowed()
    try:
        expanded = path.expanduser()
        if not expanded.is_absolute():
            raise _path_not_allowed()
        lexical = Path(os.path.abspath(expanded))
        resolved = expanded.resolve(strict=False)
        home = Path.home().resolve(strict=True)
        if resolved != lexical or resolved == Path(resolved.anchor) or resolved == home:
            raise _path_not_allowed()
        if expanded.is_symlink():
            raise _path_not_allowed()
        existing = lexical
        while True:
            try:
                existing_status = existing.stat(follow_symlinks=False)
            except FileNotFoundError as exc:
                parent = existing.parent
                if parent == existing:
                    raise _path_not_allowed() from exc
                existing = parent
                continue
            if not stat.S_ISDIR(existing_status.st_mode):
                raise _path_not_allowed()
            break
        return resolved
    except ForgeException:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _path_not_allowed(exc) from exc


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _storage_unavailable(exc) from exc


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        status = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise _path_not_allowed(exc) from exc
    if not stat.S_ISDIR(status.st_mode):
        raise _path_not_allowed()
    return status.st_dev, status.st_ino


def _status_identity(status: os.stat_result) -> _FileIdentity:
    return status.st_dev, status.st_ino


def _require_secure_directory_primitives() -> None:
    required = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir, os.link, os.rename)
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or any(function not in os.supports_dir_fd for function in required)
    ):
        raise _path_not_allowed()


def _open_directory_capability_at(
    parent_fd: int,
    name: str,
) -> tuple[int, _FileIdentity]:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode):
            raise _path_not_allowed()
        return descriptor, _status_identity(status)
    except ForgeException:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise _path_not_allowed(exc) from exc


def _open_directory_at(parent_fd: int, name: str) -> int:
    descriptor, _identity = _open_directory_capability_at(parent_fd, name)
    return descriptor


def _matches_identity(parent_fd: int, name: str, identity: _FileIdentity) -> bool:
    try:
        return _status_identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False)) == identity
    except OSError:
        return False


def _unlink_matching(
    parent_fd: int,
    name: str,
    identity: _FileIdentity | None,
) -> bool | None:
    if identity is None:
        return False
    quarantine_name = f".cleanup-{uuid4().hex}"
    quarantine_fd: int | None = None
    owned_quarantine = False
    candidate_name = "candidate"
    try:
        os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_fd)
        owned_quarantine = True
        quarantine_fd = _open_directory_at(parent_fd, quarantine_name)
        os.rename(
            name,
            candidate_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=quarantine_fd,
        )
        if _matches_identity(quarantine_fd, candidate_name, identity):
            os.unlink(candidate_name, dir_fd=quarantine_fd)
            return True
        else:
            try:
                os.link(
                    candidate_name,
                    name,
                    src_dir_fd=quarantine_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError:
                return False
            os.unlink(candidate_name, dir_fd=quarantine_fd)
            return False
    except (ForgeException, OSError):
        return None
    finally:
        if quarantine_fd is not None:
            with suppress(OSError):
                os.close(quarantine_fd)
        if owned_quarantine:
            with suppress(OSError):
                os.rmdir(quarantine_name, dir_fd=parent_fd)


def _close_book_descriptor(attempt: _ManagedImportAttempt) -> None:
    if not attempt.book_fd_closed and attempt.book_fd is not None:
        attempt.book_fd_closed = True
        with suppress(OSError):
            os.close(attempt.book_fd)


def _remove_owned_book_directory(
    books_fd: int,
    book_name: str,
    identity: _FileIdentity | None,
) -> None:
    if identity is None:
        return
    quarantine_name = f".cleanup-book-{uuid4().hex}"
    quarantine_fd: int | None = None
    candidate_fd: int | None = None
    owned_quarantine = False
    candidate_name = "candidate"
    try:
        os.mkdir(quarantine_name, mode=0o700, dir_fd=books_fd)
        owned_quarantine = True
        quarantine_fd = _open_directory_at(books_fd, quarantine_name)
        os.rename(
            book_name,
            candidate_name,
            src_dir_fd=books_fd,
            dst_dir_fd=quarantine_fd,
        )
        candidate_fd = _open_directory_at(quarantine_fd, candidate_name)
        if _status_identity(os.fstat(candidate_fd)) != identity:
            return
        os.close(candidate_fd)
        candidate_fd = None
        os.rmdir(candidate_name, dir_fd=quarantine_fd)
    except (ForgeException, OSError):
        pass
    finally:
        if candidate_fd is not None:
            with suppress(OSError):
                os.close(candidate_fd)
        if quarantine_fd is not None:
            with suppress(OSError):
                os.close(quarantine_fd)
        if owned_quarantine:
            with suppress(OSError):
                os.rmdir(quarantine_name, dir_fd=books_fd)


def _remove_attempt_files(attempt: _ManagedImportAttempt) -> None:
    remove_book_directory = attempt.owned_book_dir
    if attempt.book_fd is not None:
        _unlink_matching(attempt.book_fd, attempt.stage_name, attempt.stage_identity)
        _unlink_matching(attempt.book_fd, attempt.final_name, attempt.final_identity)
        try:
            remove_book_directory = remove_book_directory and not os.listdir(attempt.book_fd)
        except OSError:
            remove_book_directory = False
    _close_book_descriptor(attempt)
    if remove_book_directory:
        _remove_owned_book_directory(
            attempt.books_fd,
            attempt.book_name,
            attempt.book_identity,
        )


def _copy_source_to_stage(
    source: Path,
    stage: Path,
    *,
    limits: ExtractionLimits,
    source_opener: _BinaryOpener = _open_binary,
    stage_opener: _BinaryOpener = _open_binary,
) -> str:
    digest = hashlib.sha256()
    bytes_read = 0
    source_stream: BinaryIO | None = None
    target_stream: BinaryIO | None = None
    failure: BaseException | None = None
    fingerprint: str | None = None
    try:
        try:
            source_stream = source_opener(source, "rb")
        except OSError as exc:
            raise _source_changed(exc) from exc
        try:
            target_stream = stage_opener(stage, "xb")
        except OSError as exc:
            raise _storage_unavailable(exc) from exc
        while True:
            try:
                chunk = source_stream.read(
                    min(_HASH_CHUNK_SIZE, limits.max_source_bytes - bytes_read + 1)
                )
            except OSError as exc:
                raise _source_changed(exc) from exc
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > limits.max_source_bytes:
                raise _source_changed()
            digest.update(chunk)
            try:
                target_stream.write(chunk)
            except OSError as exc:
                raise _storage_unavailable(exc) from exc
        try:
            target_stream.flush()
        except OSError as exc:
            raise _storage_unavailable(exc) from exc
        fingerprint = digest.hexdigest()
    except BaseException as exc:
        failure = exc
    finally:
        if target_stream is not None:
            try:
                target_stream.close()
            except OSError as exc:
                if failure is None:
                    failure = _storage_unavailable(exc)
        if source_stream is not None:
            try:
                source_stream.close()
            except OSError as exc:
                if failure is None:
                    failure = _source_changed(exc)
    if failure is not None:
        raise failure
    if fingerprint is None:
        raise _source_changed()
    return fingerprint


class BookLibrary:
    """Persist normalized extracted books in an optional local SQLite library."""

    def __init__(
        self,
        config: AppConfig,
        *,
        registry: BookExtractorRegistry | None = None,
        repository: LibraryRepository | None = None,
    ) -> None:
        self._config = config
        self._data_root = _validate_data_root(library_data_path(config))
        self._registry = registry or BookExtractorRegistry()
        self._repository = repository or LibraryRepository(
            LibraryDatabase(self._data_root / "library.sqlite3")
        )
        self._data_root_identity: tuple[int, int] | None = None
        self._data_root_fd: int | None = None
        self._books_fd: int | None = None
        self._initialized = False

    def initialize(self) -> None:
        if self._data_root_identity is None:
            _require_secure_directory_primitives()
            if _validate_data_root(self._data_root) != self._data_root:
                raise _path_not_allowed()
            _ensure_directory(self._data_root)
            if _validate_data_root(self._data_root) != self._data_root:
                raise _path_not_allowed()
            expected_identity = _directory_identity(self._data_root)
            try:
                descriptor = os.open(
                    self._data_root,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
            except OSError as exc:
                raise _path_not_allowed(exc) from exc
            try:
                actual_identity = _status_identity(os.fstat(descriptor))
            except OSError as exc:
                os.close(descriptor)
                raise _path_not_allowed(exc) from exc
            if actual_identity != expected_identity:
                os.close(descriptor)
                raise _path_not_allowed()
            self._data_root_identity = actual_identity
            self._data_root_fd = descriptor
            self._repository.bind_database_anchor(descriptor, self._assert_data_root_identity)
        with self._data_root_guard():
            self._repository.initialize()
        self._initialized = True

    def import_book(self, source: Path, mode: ImportMode | None = None) -> ImportedBook:
        if not self._config.library.enabled:
            raise ForgeException(
                ForgeErrorCode.CONFIG_INVALID,
                "Managed library is disabled.",
            )
        self._require_initialized()
        with self._data_root_guard():
            selected_mode = mode or (
                ImportMode.COPY if self._config.library.copy_imports else ImportMode.REFERENCE
            )
            extracted = self._registry.extract(source)
            with self._data_root_guard():
                existing = self._repository.find_managed_book(
                    extracted.format,
                    extracted.source_fingerprint,
                )
            if existing is not None:
                return existing.imported

            imported = ImportedBook(
                book=BookRef(book_id=uuid4().hex),
                metadata=extracted.metadata,
                format=extracted.format,
                import_mode=selected_mode,
                source_fingerprint=extracted.source_fingerprint,
            )
            if selected_mode is ImportMode.REFERENCE:
                try:
                    reference_source = source.resolve(strict=True)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise _source_changed(exc) from exc
                record = self._repository.new_record(
                    imported,
                    managed_source_path=None,
                    reference_source_path=reference_source,
                )
                return self._persist(record, extracted.chapters).record.imported

            return self._import_copy(source, imported, extracted.chapters)

    def upsert_external_book(
        self,
        identity: ExternalIdentity,
        metadata: BookMetadata,
    ) -> BookRef:
        self._require_initialized()
        identity_json = _canonical_json(identity.model_dump(mode="json"))
        with self._data_root_guard():
            record = self._repository.upsert_external_book(
                identity,
                metadata,
                candidate=BookRef(book_id=uuid4().hex),
                source_fingerprint=_canonical_fingerprint(identity_json),
            )
        return record.book

    def upsert_chapter_snapshot(self, snapshot: ChapterSnapshot) -> BookRef:
        self._require_initialized()
        snapshot_json = _canonical_json(snapshot.model_dump(mode="json"))
        identity_json = _canonical_json(snapshot.external_identity.model_dump(mode="json"))
        with self._data_root_guard():
            record = self._repository.upsert_chapter_snapshot(
                snapshot,
                candidate=BookRef(book_id=uuid4().hex),
                book_fingerprint=_canonical_fingerprint(identity_json),
                snapshot_json=snapshot_json,
                content_fingerprint=_canonical_fingerprint(snapshot_json),
            )
        return record.book

    def list_books(self) -> tuple[StoredBook, ...]:
        self._require_initialized()
        with self._data_root_guard():
            return tuple(self._to_stored(record) for record in self._repository.list_books())

    def get_book(self, book: BookRef) -> StoredBook:
        self._require_initialized()
        with self._data_root_guard():
            return self._to_stored(self._repository.get_book(book))

    def get_chapter(self, book: BookRef, index: int) -> ChapterContent:
        self._require_initialized()
        with self._data_root_guard():
            return self._repository.get_chapter(book, index)

    def _import_copy(
        self,
        source: Path,
        imported: ImportedBook,
        chapters: tuple[ChapterContent, ...],
    ) -> ImportedBook:
        attempt: _ManagedImportAttempt | None = None
        try:
            books_fd = self._managed_books_descriptor()
            book_name = imported.book.book_id
            extension = imported.format.value
            stage_name = f".source-{uuid4().hex}.tmp"
            final_name = f"source.{extension}"
            owned_book_dir = False
            try:
                os.mkdir(book_name, mode=0o700, dir_fd=books_fd)
                owned_book_dir = True
            except FileExistsError:
                pass
            except OSError as exc:
                raise _storage_unavailable(exc) from exc
            attempt = _ManagedImportAttempt(
                books_fd=books_fd,
                book_fd=None,
                book_name=book_name,
                stage_name=stage_name,
                final_name=final_name,
                book_identity=None,
                owned_book_dir=owned_book_dir,
            )
            attempt.book_fd, attempt.book_identity = _open_directory_capability_at(
                books_fd,
                book_name,
            )
            try:
                book_status = os.stat(book_name, dir_fd=books_fd, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(book_status.st_mode)
                    or _status_identity(book_status) != attempt.book_identity
                ):
                    raise _path_not_allowed()
            except OSError as exc:
                raise _storage_unavailable(exc) from exc
            stage = self._data_root / "books" / book_name / stage_name

            def open_anchored_stage(_path: Path, mode: str) -> BinaryIO:
                if mode != "xb" or attempt is None:
                    raise OSError("invalid managed stage mode")
                if attempt.book_fd is None:
                    raise OSError("managed book directory is unavailable")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                descriptor = os.open(
                    attempt.stage_name,
                    flags,
                    0o600,
                    dir_fd=attempt.book_fd,
                )
                try:
                    attempt.stage_identity = _status_identity(os.fstat(descriptor))
                    return cast(BinaryIO, os.fdopen(descriptor, "wb"))
                except BaseException:
                    with suppress(OSError):
                        os.close(descriptor)
                    raise

            with self._data_root_guard():
                copied_fingerprint = _copy_source_to_stage(
                    source,
                    stage,
                    limits=self._registry.limits,
                    stage_opener=open_anchored_stage,
                )
            if copied_fingerprint != imported.source_fingerprint:
                raise _source_changed()
            relative_source = f"books/{book_name}/{final_name}"
            record = self._repository.new_record(
                imported,
                managed_source_path=relative_source,
                reference_source_path=None,
            )

            def publish() -> None:
                if attempt is None or attempt.book_fd is None or attempt.stage_identity is None:
                    raise _path_not_allowed()
                with self._data_root_guard():
                    try:
                        os.link(
                            attempt.stage_name,
                            attempt.final_name,
                            src_dir_fd=attempt.book_fd,
                            dst_dir_fd=attempt.book_fd,
                            follow_symlinks=False,
                        )
                    except FileExistsError as exc:
                        raise _path_not_allowed(exc) from exc
                    except OSError as exc:
                        raise _storage_unavailable(exc) from exc
                    attempt.final_identity = attempt.stage_identity
                    stage_removal = _unlink_matching(
                        attempt.book_fd,
                        attempt.stage_name,
                        attempt.stage_identity,
                    )
                    if stage_removal is None:
                        raise _storage_unavailable(OSError("managed stage cleanup failed"))
                    attempt.stage_identity = None

            persisted = self._persist(record, chapters, publish=publish)
            if not persisted.created:
                self._cleanup_import_attempt(attempt)
            else:
                _close_book_descriptor(attempt)
            return persisted.record.imported
        except BaseException:
            self._cleanup_import_attempt(attempt)
            raise

    def _cleanup_import_attempt(
        self,
        attempt: _ManagedImportAttempt | None,
    ) -> None:
        if attempt is not None:
            _remove_attempt_files(attempt)

    def _managed_books_directory(self) -> Path:
        with self._data_root_guard():
            root_fd = self._data_root_fd
            if root_fd is None:
                raise _path_not_allowed()
            if self._books_fd is None:
                try:
                    os.mkdir("books", mode=0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise _storage_unavailable(exc) from exc
                self._books_fd = _open_directory_at(root_fd, "books")
            return self._data_root / "books"

    def _managed_books_descriptor(self) -> int:
        self._managed_books_directory()
        books_fd = self._books_fd
        if books_fd is None:
            raise _path_not_allowed()
        return books_fd

    def _managed_source_fingerprint(self, book_name: str, source_name: str) -> str:
        books_fd = self._managed_books_descriptor()
        book_fd = _open_directory_at(books_fd, book_name)
        source_fd: int | None = None
        try:
            try:
                source_fd = os.open(
                    source_name,
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=book_fd,
                )
                source_status = os.fstat(source_fd)
                if (
                    not stat.S_ISREG(source_status.st_mode)
                    or source_status.st_size > self._registry.limits.max_source_bytes
                ):
                    raise _source_changed()
                digest = hashlib.sha256()
                bytes_read = 0
                while True:
                    chunk = os.read(
                        source_fd,
                        min(
                            _HASH_CHUNK_SIZE,
                            self._registry.limits.max_source_bytes - bytes_read + 1,
                        ),
                    )
                    if not chunk:
                        return digest.hexdigest()
                    bytes_read += len(chunk)
                    if bytes_read > self._registry.limits.max_source_bytes:
                        raise _source_changed()
                    digest.update(chunk)
            except OSError as exc:
                raise _source_changed(exc) from exc
        finally:
            if source_fd is not None:
                with suppress(OSError):
                    os.close(source_fd)
            with suppress(OSError):
                os.close(book_fd)

    def _persist(
        self,
        record: LibraryBookRecord,
        chapters: tuple[ChapterContent, ...],
        *,
        publish: Callable[[], None] | None = None,
    ) -> PersistedBook:
        try:
            with self._data_root_guard():
                return self._repository.persist_book(record, chapters, publish=publish)
        except ForgeException:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise _storage_unavailable(exc) from exc

    def _to_stored(self, record: LibraryRecord) -> StoredBook:
        if isinstance(record, ExternalBookRecord):
            return StoredBook(
                book=record.book,
                metadata=record.metadata,
                source_fingerprint=record.source_fingerprint,
                source_available=True,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        imported = record.imported
        return StoredBook(
            book=imported.book,
            metadata=imported.metadata,
            format=imported.format,
            import_mode=imported.import_mode,
            source_fingerprint=imported.source_fingerprint,
            source_available=self._source_available(record),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    def _source_available(self, record: LibraryBookRecord) -> bool:
        with self._data_root_guard():
            imported = record.imported
            if imported.import_mode is ImportMode.COPY:
                expected = f"books/{imported.book.book_id}/source.{imported.format.value}"
                if record.managed_source_path != expected:
                    return False
                try:
                    return (
                        self._managed_source_fingerprint(
                            imported.book.book_id,
                            f"source.{imported.format.value}",
                        )
                        == imported.source_fingerprint
                    )
                except ForgeException:
                    return False
            else:
                if record.reference_source_path is None:
                    return False
                source = record.reference_source_path
            try:
                return (
                    stat.S_ISREG(source.stat(follow_symlinks=False).st_mode)
                    and fingerprint_source(source, limits=self._registry.limits)
                    == imported.source_fingerprint
                )
            except (ForgeException, OSError, RuntimeError, ValueError):
                return False

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise ForgeException(
                ForgeErrorCode.CONFIG_INVALID,
                "Library must be initialized before use.",
            )
        self._assert_data_root_identity()

    def _assert_data_root_identity(self) -> None:
        identity = self._data_root_identity
        descriptor = self._data_root_fd
        if identity is None or descriptor is None:
            raise _path_not_allowed()
        try:
            if (
                _status_identity(os.fstat(descriptor)) != identity
                or _directory_identity(self._data_root) != identity
            ):
                raise _path_not_allowed()
        except ForgeException:
            raise
        except OSError as exc:
            raise _path_not_allowed(exc) from exc

    @contextmanager
    def _data_root_guard(self) -> Iterator[None]:
        self._assert_data_root_identity()
        try:
            yield
        finally:
            self._assert_data_root_identity()


def create_book_library(
    config: AppConfig,
    *,
    registry: BookExtractorRegistry | None = None,
    repository: LibraryRepository | None = None,
) -> BookLibrary:
    """Construct and explicitly initialize a managed library service."""
    library = BookLibrary(config, registry=registry, repository=repository)
    library.initialize()
    return library
