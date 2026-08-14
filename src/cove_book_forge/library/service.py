import hashlib
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO, cast
from uuid import uuid4

from cove_book_forge.config import AppConfig, library_data_path
from cove_book_forge.contracts import (
    BookRef,
    ChapterContent,
    ImportedBook,
    ImportMode,
    StoredBook,
)
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors import BookExtractorRegistry
from cove_book_forge.extractors.security import ExtractionLimits, fingerprint_source
from cove_book_forge.library.database import LibraryDatabase
from cove_book_forge.library.repository import (
    LibraryBookRecord,
    LibraryRepository,
    PersistedBook,
)

_HASH_CHUNK_SIZE = 1024 * 1024
_BinaryOpener = Callable[[Path, str], BinaryIO]


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
        if expanded.exists() and not expanded.is_dir():
            raise _path_not_allowed()
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


def _remove_attempt_files(
    stage: Path | None,
    final: Path | None,
    book_dir: Path | None,
    *,
    published_final: bool,
    owned_book_dir: bool,
) -> None:
    if stage is not None:
        try:
            if stage.is_file() or stage.is_symlink():
                stage.unlink()
        except OSError:
            pass
    if published_final and final is not None:
        try:
            if final.is_file() or final.is_symlink():
                final.unlink()
        except OSError:
            pass
    if owned_book_dir and book_dir is not None:
        with suppress(OSError):
            book_dir.rmdir()


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
        self._initialized = False

    def initialize(self) -> None:
        if self._data_root_identity is None:
            if _validate_data_root(self._data_root) != self._data_root:
                raise _path_not_allowed()
            _ensure_directory(self._data_root)
            if _validate_data_root(self._data_root) != self._data_root:
                raise _path_not_allowed()
            self._data_root_identity = _directory_identity(self._data_root)
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
        book_dir: Path | None = None
        stage: Path | None = None
        final: Path | None = None
        owned_book_dir = False
        published_final = False
        try:
            books_dir = self._managed_books_directory()
            with self._data_root_guard():
                book_dir = books_dir / imported.book.book_id
                try:
                    book_dir.mkdir()
                    owned_book_dir = True
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise _storage_unavailable(exc) from exc
                if book_dir.is_symlink() or book_dir.resolve(strict=True).parent != books_dir:
                    raise _path_not_allowed()
            extension = imported.format.value
            stage = book_dir / f".source-{uuid4().hex}.tmp"
            final = book_dir / f"source.{extension}"
            with self._data_root_guard():
                copied_fingerprint = _copy_source_to_stage(
                    source,
                    stage,
                    limits=self._registry.limits,
                )
            if copied_fingerprint != imported.source_fingerprint:
                raise _source_changed()
            relative_source = final.relative_to(self._data_root).as_posix()
            record = self._repository.new_record(
                imported,
                managed_source_path=relative_source,
                reference_source_path=None,
            )

            def publish() -> None:
                nonlocal published_final
                if final is None or stage is None:
                    raise _path_not_allowed()
                with self._data_root_guard():
                    try:
                        os.link(stage, final)
                    except FileExistsError as exc:
                        raise _path_not_allowed(exc) from exc
                    except OSError as exc:
                        raise _storage_unavailable(exc) from exc
                    published_final = True
                    try:
                        stage.unlink()
                    except OSError as exc:
                        raise _storage_unavailable(exc) from exc

            persisted = self._persist(record, chapters, publish=publish)
            if not persisted.created:
                self._cleanup_import_attempt(
                    stage,
                    final,
                    book_dir,
                    published_final=published_final,
                    owned_book_dir=owned_book_dir,
                )
            return persisted.record.imported
        except BaseException:
            self._cleanup_import_attempt(
                stage,
                final,
                book_dir,
                published_final=published_final,
                owned_book_dir=owned_book_dir,
            )
            raise

    def _cleanup_import_attempt(
        self,
        stage: Path | None,
        final: Path | None,
        book_dir: Path | None,
        *,
        published_final: bool,
        owned_book_dir: bool,
    ) -> None:
        try:
            with self._data_root_guard():
                _remove_attempt_files(
                    stage,
                    final,
                    book_dir,
                    published_final=published_final,
                    owned_book_dir=owned_book_dir,
                )
        except ForgeException:
            pass

    def _managed_books_directory(self) -> Path:
        with self._data_root_guard():
            books_dir = self._data_root / "books"
            if books_dir.is_symlink():
                raise _path_not_allowed()
            _ensure_directory(books_dir)
            try:
                resolved = books_dir.resolve(strict=True)
            except (OSError, RuntimeError, ValueError) as exc:
                raise _path_not_allowed(exc) from exc
            if resolved != books_dir or resolved.parent != self._data_root:
                raise _path_not_allowed()
            return resolved

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

    def _to_stored(self, record: LibraryBookRecord) -> StoredBook:
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
                source = self._data_root / expected
                try:
                    resolved = source.resolve(strict=True)
                except (OSError, RuntimeError, ValueError):
                    return False
                if self._data_root not in resolved.parents:
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
        if identity is None or _directory_identity(self._data_root) != identity:
            raise _path_not_allowed()

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
