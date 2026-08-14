import hashlib
import os
import sqlite3
import stat
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
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


def _remove_attempt_files(stage: Path | None, final: Path | None, book_dir: Path | None) -> None:
    for path in (stage, final):
        if path is None:
            continue
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
        except OSError:
            pass
    if book_dir is not None:
        with suppress(OSError):
            book_dir.rmdir()
        with suppress(OSError):
            book_dir.parent.rmdir()


def _copy_source_to_stage(
    source: Path,
    stage: Path,
    *,
    limits: ExtractionLimits,
) -> str:
    digest = hashlib.sha256()
    bytes_read = 0
    try:
        source_stream = source.open("rb")
    except OSError as exc:
        raise _source_changed(exc) from exc
    try:
        try:
            target_stream = stage.open("xb")
        except OSError as exc:
            raise _storage_unavailable(exc) from exc
        with target_stream:
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
    finally:
        source_stream.close()
    return digest.hexdigest()


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
        self._initialized = False

    def initialize(self) -> None:
        if _validate_data_root(self._data_root) != self._data_root:
            raise _path_not_allowed()
        _ensure_directory(self._data_root)
        if _validate_data_root(self._data_root) != self._data_root:
            raise _path_not_allowed()
        self._repository.initialize()
        self._initialized = True

    def import_book(self, source: Path, mode: ImportMode | None = None) -> ImportedBook:
        if not self._config.library.enabled:
            raise ForgeException(
                ForgeErrorCode.CONFIG_INVALID,
                "Managed library is disabled.",
            )
        self._require_initialized()
        selected_mode = mode or (
            ImportMode.COPY if self._config.library.copy_imports else ImportMode.REFERENCE
        )
        extracted = self._registry.extract(source)
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
        return tuple(self._to_stored(record) for record in self._repository.list_books())

    def get_book(self, book: BookRef) -> StoredBook:
        self._require_initialized()
        return self._to_stored(self._repository.get_book(book))

    def get_chapter(self, book: BookRef, index: int) -> ChapterContent:
        self._require_initialized()
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
        try:
            books_dir = self._managed_books_directory()
            book_dir = books_dir / imported.book.book_id
            _ensure_directory(book_dir)
            if book_dir.is_symlink() or book_dir.resolve(strict=True).parent != books_dir:
                raise _path_not_allowed()
            extension = imported.format.value
            stage = book_dir / f".source-{uuid4().hex}.tmp"
            final = book_dir / f"source.{extension}"
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
                if final is None or stage is None or final.exists() or final.is_symlink():
                    raise _path_not_allowed()
                try:
                    os.replace(stage, final)
                except OSError as exc:
                    raise _storage_unavailable(exc) from exc

            persisted = self._persist(record, chapters, publish=publish)
            if not persisted.created:
                _remove_attempt_files(stage, final, book_dir)
            return persisted.record.imported
        except BaseException:
            _remove_attempt_files(stage, final, book_dir)
            raise

    def _managed_books_directory(self) -> Path:
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
