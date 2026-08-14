import io
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

from cove_book_forge.config import AppConfig
from cove_book_forge.contracts import (
    BookFormat,
    BookMetadata,
    BookRef,
    ChapterContent,
    ExtractedBook,
    ImportMode,
)
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors import BookExtractorRegistry
from cove_book_forge.extractors.security import ExtractionLimits
from cove_book_forge.library import BookLibrary, LibraryDatabase, LibraryRepository
from cove_book_forge.library import service as library_service


class SyntheticPdfExtractor:
    def __init__(self, chapters: tuple[ChapterContent, ...] | None = None) -> None:
        self._chapters = chapters or (
            ChapterContent(index=1, title="Second", content="Second content.", source_locator="p2"),
            ChapterContent(index=0, title="First", content="First content.", source_locator="p1"),
        )

    def extract(self, source: Path, fingerprint: str) -> ExtractedBook:
        return ExtractedBook(
            format=BookFormat.PDF,
            metadata=BookMetadata(
                title="../../ Metadata must not become a path",
                author="Fixture Author",
                language="en",
                total_chapters=len(self._chapters),
            ),
            chapters=self._chapters,
            source_fingerprint=fingerprint,
        )


class FailingCommitDatabase(LibraryDatabase):
    fail_before_commit = False

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with super().transaction() as connection:
            yield connection
            if self.fail_before_commit:
                raise sqlite3.OperationalError("INSERT private SQL failed before commit")


class ReplaceRootBeforeConnectDatabase(LibraryDatabase):
    def __init__(
        self,
        path: Path,
        *,
        data_root: Path,
        original_root: Path,
        outside: Path,
    ) -> None:
        super().__init__(path)
        self._data_root = data_root
        self._original_root = original_root
        self._outside = outside
        self.armed = False

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self.armed:
            self.armed = False
            self._data_root.rename(self._original_root)
            self._data_root.symlink_to(self._outside, target_is_directory=True)
        with super().connect() as connection:
            yield connection


class FlushFailingStream(io.BytesIO):
    def flush(self) -> None:
        raise OSError("flush failed at /private/stage.pdf")


class CloseFailingStream(io.BytesIO):
    def close(self) -> None:
        if not self.closed:
            super().close()
            raise OSError("close failed at /private/stage.pdf")


class ReadAndCloseFailingStream(io.BytesIO):
    def __init__(self, initial_bytes: bytes) -> None:
        super().__init__(initial_bytes)
        self.read_error = OSError("read failed at /private/source.pdf")
        self.close_error = OSError("close failed at /private/source.pdf")

    def read(self, size: int = -1) -> bytes:
        raise self.read_error

    def close(self) -> None:
        if not self.closed:
            super().close()
            raise self.close_error


def _config(
    data_dir: Path,
    *,
    enabled: bool = True,
    copy_imports: bool = True,
) -> AppConfig:
    return AppConfig.model_validate(
        {
            "library": {
                "enabled": enabled,
                "copy_imports": copy_imports,
                "data_dir": data_dir,
            },
            "model": {"provider": "test", "model": "test"},
        }
    )


def _source(path: Path, payload: bytes = b"%PDF-1.7\nsynthetic") -> Path:
    path.write_bytes(payload)
    return path


def _registry(*, limits: ExtractionLimits | None = None) -> BookExtractorRegistry:
    return BookExtractorRegistry(
        {BookFormat.PDF: SyntheticPdfExtractor()},
        limits=limits,
    )


def _initialized_library(
    data_dir: Path,
    *,
    registry: BookExtractorRegistry | None = None,
    copy_imports: bool = True,
    repository: LibraryRepository | None = None,
) -> BookLibrary:
    library = BookLibrary(
        _config(data_dir, copy_imports=copy_imports),
        registry=registry or _registry(),
        repository=repository,
    )
    library.initialize()
    return library


def test_initialization_is_explicit_and_default_mode_comes_from_config(tmp_path: Path) -> None:
    data_dir = tmp_path / "library"
    source = _source(tmp_path / "book.pdf")
    library = BookLibrary(_config(data_dir, copy_imports=False), registry=_registry())

    assert not data_dir.exists()
    library.initialize()
    imported = library.import_book(source)

    assert imported.import_mode is ImportMode.REFERENCE
    assert data_dir.is_dir()
    assert not (data_dir / "books").exists()


def test_copy_import_is_idempotent_and_uses_only_random_internal_paths(tmp_path: Path) -> None:
    data_dir = tmp_path / "library"
    source = _source(tmp_path / "book.pdf")
    library = _initialized_library(data_dir)

    first = library.import_book(source, ImportMode.COPY)
    second = library.import_book(source, ImportMode.COPY)

    expected_source = data_dir / "books" / first.book.book_id / "source.pdf"
    assert first == second
    assert first.book.book_id != first.metadata.title
    assert expected_source.read_bytes() == source.read_bytes()
    assert tuple(path for path in (data_dir / "books").rglob("source.pdf")) == (expected_source,)
    assert library.list_books() == (library.get_book(first.book),)


def test_chapters_round_trip_in_index_order_and_unknown_refs_are_safe(tmp_path: Path) -> None:
    source = _source(tmp_path / "book.pdf")
    library = _initialized_library(tmp_path / "library")
    imported = library.import_book(source, ImportMode.REFERENCE)

    assert library.get_chapter(imported.book, 0) == ChapterContent(
        index=0,
        title="First",
        content="First content.",
        source_locator="p1",
    )
    assert library.get_chapter(imported.book, 1).title == "Second"

    for operation in (
        lambda: library.get_book(BookRef(book_id="missing")),
        lambda: library.get_chapter(imported.book, 99),
        lambda: library.get_chapter(BookRef(book_id="missing"), 0),
    ):
        with pytest.raises(ForgeException) as exc_info:
            operation()
        assert exc_info.value.code is ForgeErrorCode.SOURCE_NOT_FOUND
        assert str(tmp_path) not in str(exc_info.value)


def test_reference_import_stores_the_strict_target_without_copying(tmp_path: Path) -> None:
    target = _source(tmp_path / "target.pdf")
    source_link = tmp_path / "linked.pdf"
    source_link.symlink_to(target)
    data_dir = tmp_path / "library"
    library = _initialized_library(data_dir)

    imported = library.import_book(source_link, ImportMode.REFERENCE)
    source_link.unlink()

    assert library.get_book(imported.book).source_available is True
    assert library.get_chapter(imported.book, 0).content == "First content."
    assert not (data_dir / "books").exists()


@pytest.mark.parametrize("mutation", ["missing", "directory", "changed", "oversized", "symlink"])
def test_reference_availability_revalidates_the_current_source(
    tmp_path: Path,
    mutation: str,
) -> None:
    limits = ExtractionLimits(max_source_bytes=64)
    source = _source(tmp_path / "book.pdf")
    library = _initialized_library(tmp_path / "library", registry=_registry(limits=limits))
    imported = library.import_book(source, ImportMode.REFERENCE)

    if mutation == "missing":
        source.unlink()
    elif mutation == "directory":
        source.unlink()
        source.mkdir()
    elif mutation == "changed":
        source.write_bytes(b"%PDF-1.7\ndifferent")
    elif mutation == "oversized":
        source.write_bytes(b"%PDF-" + b"x" * 65)
    else:
        replacement = _source(tmp_path / "replacement.pdf", source.read_bytes())
        source.unlink()
        source.symlink_to(replacement)

    assert library.get_book(imported.book).source_available is False
    assert library.list_books()[0].source_available is False
    assert library.get_chapter(imported.book, 1).content == "Second content."


def test_managed_source_availability_detects_deletion_and_tampering(tmp_path: Path) -> None:
    data_dir = tmp_path / "library"
    source = _source(tmp_path / "book.pdf")
    library = _initialized_library(data_dir)
    imported = library.import_book(source, ImportMode.COPY)
    managed = data_dir / "books" / imported.book.book_id / "source.pdf"

    managed.write_bytes(b"%PDF-1.7\ntampered")
    assert library.get_book(imported.book).source_available is False
    assert library.get_chapter(imported.book, 0).content == "First content."

    managed.unlink()
    assert library.get_book(imported.book).source_available is False

    same_bytes = managed.with_name("same-bytes.pdf")
    same_bytes.write_bytes(source.read_bytes())
    managed.symlink_to(same_bytes)
    assert library.get_book(imported.book).source_available is False


def test_explicit_mode_overrides_the_configured_default(tmp_path: Path) -> None:
    data_dir = tmp_path / "library"
    source = _source(tmp_path / "book.pdf")
    library = _initialized_library(data_dir, copy_imports=False)

    imported = library.import_book(source, ImportMode.COPY)

    assert imported.import_mode is ImportMode.COPY
    assert (data_dir / "books" / imported.book.book_id / "source.pdf").is_file()


def test_disabled_library_rejects_before_extraction_or_storage(tmp_path: Path) -> None:
    data_dir = tmp_path / "disabled-library"
    missing_source = tmp_path / "missing.pdf"
    library = BookLibrary(_config(data_dir, enabled=False), registry=_registry())

    with pytest.raises(ForgeException) as exc_info:
        library.import_book(missing_source)

    assert exc_info.value.code is ForgeErrorCode.CONFIG_INVALID
    assert not data_dir.exists()
    assert str(data_dir) not in str(exc_info.value)


def test_unsafe_data_roots_and_managed_tree_symlinks_are_rejected(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory", encoding="utf-8")
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)

    for unsafe in (
        Path(unsafe_path) for unsafe_path in (Path("/"), Path.home(), occupied, linked_root)
    ):
        with pytest.raises(ForgeException) as exc_info:
            BookLibrary(_config(unsafe), registry=_registry())
        assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
        assert str(unsafe) not in str(exc_info.value)

    nul_root = Path(str(tmp_path / "nul") + "\x00private")
    with pytest.raises(ForgeException) as exc_info:
        BookLibrary(_config(nul_root), registry=_registry())
    assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED

    replaced_root = tmp_path / "replaced-after-construction"
    replaced_library = BookLibrary(_config(replaced_root), registry=_registry())
    replaced_root.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ForgeException) as exc_info:
        replaced_library.initialize()
    assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED

    data_dir = tmp_path / "library"
    outside = tmp_path / "outside"
    outside.mkdir()
    library = _initialized_library(data_dir)
    (data_dir / "books").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ForgeException) as exc_info:
        library.import_book(_source(tmp_path / "book.pdf"), ImportMode.COPY)
    assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert tuple(outside.iterdir()) == ()


@pytest.mark.parametrize("operation", ["list", "get", "reference_import"])
def test_initialized_data_root_identity_is_required_for_every_database_operation(
    tmp_path: Path,
    operation: str,
) -> None:
    data_dir = tmp_path / "library"
    library = _initialized_library(data_dir)
    imported = library.import_book(_source(tmp_path / "first.pdf"), ImportMode.REFERENCE)

    original_root = tmp_path / "original-library"
    outside = tmp_path / "outside"
    outside.mkdir()
    data_dir.rename(original_root)
    data_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ForgeException) as exc_info:
        if operation == "list":
            library.list_books()
        elif operation == "get":
            library.get_book(imported.book)
        else:
            library.import_book(
                _source(tmp_path / "second.pdf", b"%PDF-1.7\nsecond"),
                ImportMode.REFERENCE,
            )

    assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert tuple(outside.iterdir()) == ()


@pytest.mark.parametrize("operation", ["list", "reference", "copy"])
def test_database_operation_is_anchored_when_root_changes_after_the_guard(
    tmp_path: Path,
    operation: str,
) -> None:
    data_dir = tmp_path / "library"
    original_root = tmp_path / "original-library"
    outside = tmp_path / "outside"
    outside.mkdir()
    database = ReplaceRootBeforeConnectDatabase(
        data_dir / "library.sqlite3",
        data_root=data_dir,
        original_root=original_root,
        outside=outside,
    )
    library = _initialized_library(
        data_dir,
        repository=LibraryRepository(database),
    )
    database.armed = True

    with pytest.raises(ForgeException) as exc_info:
        if operation == "list":
            library.list_books()
        else:
            library.import_book(
                _source(tmp_path / f"{operation}.pdf"),
                ImportMode(operation),
            )

    assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert tuple(outside.iterdir()) == ()


def test_data_root_replacement_before_managed_book_directory_write_creates_nothing_outside(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "library"
    original_root = tmp_path / "original-library"
    outside = tmp_path / "outside"
    outside_books = outside / "books"
    outside_books.mkdir(parents=True)

    class ReplaceRootAfterBooksLookupLibrary(BookLibrary):
        armed = False

        def _managed_books_directory(self) -> Path:
            books_dir = super()._managed_books_directory()
            if self.armed:
                data_dir.rename(original_root)
                data_dir.symlink_to(outside, target_is_directory=True)
            return books_dir

    library = ReplaceRootAfterBooksLookupLibrary(_config(data_dir), registry=_registry())
    library.initialize()
    library.armed = True

    with pytest.raises(ForgeException) as exc_info:
        library.import_book(_source(tmp_path / "book.pdf"), ImportMode.COPY)

    assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert tuple(outside_books.iterdir()) == ()


def test_copy_stage_open_is_anchored_when_root_changes_after_the_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "library"
    original_root = tmp_path / "original-library"
    outside = tmp_path / "outside"
    fixed_uuid = UUID(int=20)
    outside_book_dir = outside / "books" / fixed_uuid.hex
    outside_book_dir.mkdir(parents=True)
    library = _initialized_library(data_dir)
    monkeypatch.setattr(library_service, "uuid4", lambda: fixed_uuid)
    real_copy = library_service._copy_source_to_stage

    def replace_root_then_copy(*args: object, **kwargs: object) -> str:
        data_dir.rename(original_root)
        data_dir.symlink_to(outside, target_is_directory=True)
        return real_copy(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library_service, "_copy_source_to_stage", replace_root_then_copy)

    with pytest.raises(ForgeException) as exc_info:
        library.import_book(_source(tmp_path / "book.pdf"), ImportMode.COPY)

    assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert tuple(outside_book_dir.iterdir()) == ()


def test_copy_publish_is_anchored_when_root_changes_immediately_before_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "library"
    original_root = tmp_path / "original-library"
    outside = tmp_path / "outside"
    fixed_uuid = UUID(int=21)
    outside_book_dir = outside / "books" / fixed_uuid.hex
    outside_book_dir.mkdir(parents=True)
    outside_stage = outside_book_dir / f".source-{fixed_uuid.hex}.tmp"
    outside_stage.write_bytes(b"competitor-stage")
    outside_final = outside_book_dir / "source.pdf"
    library = _initialized_library(data_dir)
    monkeypatch.setattr(library_service, "uuid4", lambda: fixed_uuid)
    real_link = library_service.os.link

    def replace_root_then_link(*args: object, **kwargs: object) -> None:
        data_dir.rename(original_root)
        data_dir.symlink_to(outside, target_is_directory=True)
        real_link(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library_service.os, "link", replace_root_then_link)

    with pytest.raises(ForgeException) as exc_info:
        library.import_book(_source(tmp_path / "book.pdf"), ImportMode.COPY)

    assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert outside_stage.read_bytes() == b"competitor-stage"
    assert not outside_final.exists()


def test_source_change_during_copy_persists_nothing(tmp_path: Path) -> None:
    source = _source(tmp_path / "book.pdf")
    data_dir = tmp_path / "library"

    class ChangeAfterExtractionRegistry(BookExtractorRegistry):
        def extract(self, source: Path) -> ExtractedBook:
            extracted = super().extract(source)
            source.write_bytes(b"%PDF-1.7\nchanged-after-extraction")
            return extracted

    registry = ChangeAfterExtractionRegistry({BookFormat.PDF: SyntheticPdfExtractor()})
    library = _initialized_library(data_dir, registry=registry)

    with pytest.raises(ForgeException) as exc_info:
        library.import_book(source, ImportMode.COPY)

    assert exc_info.value.code is ForgeErrorCode.SOURCE_CHANGED
    assert library.list_books() == ()
    assert not (data_dir / "books").exists() or not tuple((data_dir / "books").rglob("source.pdf"))


def test_copy_publish_preserves_a_preexisting_final_and_book_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "library"
    source = _source(tmp_path / "book.pdf")
    library = _initialized_library(data_dir)
    fixed_uuid = UUID(int=1)
    monkeypatch.setattr(library_service, "uuid4", lambda: fixed_uuid)
    book_dir = data_dir / "books" / fixed_uuid.hex
    book_dir.mkdir(parents=True)
    final = book_dir / "source.pdf"
    competitor = b"competitor-owned"
    final.write_bytes(competitor)

    with pytest.raises(ForgeException) as exc_info:
        library.import_book(source, ImportMode.COPY)

    assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert final.read_bytes() == competitor
    assert book_dir.is_dir()
    assert library.list_books() == ()


def test_copy_publish_race_never_overwrites_or_deletes_the_competing_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "library"
    source = _source(tmp_path / "book.pdf")
    library = _initialized_library(data_dir)
    fixed_uuid = UUID(int=2)
    monkeypatch.setattr(library_service, "uuid4", lambda: fixed_uuid)
    final = data_dir / "books" / fixed_uuid.hex / "source.pdf"
    competitor = b"racing-competitor"
    real_replace = library_service.os.replace
    real_link = library_service.os.link

    def racing_replace(source_path: Path, destination: Path) -> None:
        Path(destination).write_bytes(competitor)
        real_replace(source_path, destination)

    def racing_link(source_path: object, destination: object, **kwargs: object) -> None:
        destination_fd = kwargs.get("dst_dir_fd")
        flags = library_service.os.O_WRONLY | library_service.os.O_CREAT | library_service.os.O_EXCL
        fd = library_service.os.open(destination, flags, 0o600, dir_fd=destination_fd)  # type: ignore[arg-type]
        try:
            library_service.os.write(fd, competitor)
        finally:
            library_service.os.close(fd)
        real_link(source_path, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(library_service.os, "replace", racing_replace)
    monkeypatch.setattr(library_service.os, "link", racing_link)

    with pytest.raises(ForgeException) as exc_info:
        library.import_book(source, ImportMode.COPY)

    assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert final.read_bytes() == competitor
    assert library.list_books() == ()


def test_persistence_failure_rolls_back_and_cleans_published_copy(tmp_path: Path) -> None:
    data_dir = tmp_path / "library"
    source = _source(tmp_path / "book.pdf")
    database = FailingCommitDatabase(data_dir / "library.sqlite3")
    repository = LibraryRepository(database)
    library = _initialized_library(data_dir, repository=repository)
    database.fail_before_commit = True

    with pytest.raises(ForgeException) as exc_info:
        library.import_book(source, ImportMode.COPY)

    assert exc_info.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert "INSERT" not in str(exc_info.value)
    assert str(data_dir) not in str(exc_info.value)
    assert library.list_books() == ()
    assert not (data_dir / "books").exists() or not tuple((data_dir / "books").rglob("source.pdf"))


def test_commit_failure_preserves_a_competitor_that_replaces_the_published_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "library"
    fixed_uuid = UUID(int=22)
    monkeypatch.setattr(library_service, "uuid4", lambda: fixed_uuid)
    database = FailingCommitDatabase(data_dir / "library.sqlite3")
    library = _initialized_library(data_dir, repository=LibraryRepository(database))
    final = data_dir / "books" / fixed_uuid.hex / "source.pdf"
    competitor = b"competitor-replaced-final"
    real_link = library_service.os.link

    def replace_published_final(*args: object, **kwargs: object) -> None:
        real_link(*args, **kwargs)  # type: ignore[arg-type]
        destination = args[1]
        destination_fd = kwargs.get("dst_dir_fd")
        library_service.os.unlink(destination, dir_fd=destination_fd)  # type: ignore[arg-type]
        flags = library_service.os.O_WRONLY | library_service.os.O_CREAT | library_service.os.O_EXCL
        fd = library_service.os.open(destination, flags, 0o600, dir_fd=destination_fd)  # type: ignore[arg-type]
        try:
            library_service.os.write(fd, competitor)
        finally:
            library_service.os.close(fd)

    monkeypatch.setattr(library_service.os, "link", replace_published_final)
    database.fail_before_commit = True

    with pytest.raises(ForgeException) as exc_info:
        library.import_book(_source(tmp_path / "book.pdf"), ImportMode.COPY)

    assert exc_info.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert final.read_bytes() == competitor
    assert library.list_books() == ()


def test_commit_failure_preserves_a_competitor_that_reoccupies_the_stage_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "library"
    fixed_uuid = UUID(int=23)
    monkeypatch.setattr(library_service, "uuid4", lambda: fixed_uuid)
    database = FailingCommitDatabase(data_dir / "library.sqlite3")
    library = _initialized_library(data_dir, repository=LibraryRepository(database))
    book_dir = data_dir / "books" / fixed_uuid.hex
    stage = book_dir / f".source-{fixed_uuid.hex}.tmp"
    final = book_dir / "source.pdf"
    competitor = b"competitor-reoccupied-stage"
    real_rename = library_service.os.rename
    replaced = False

    def replace_stage_once(
        source_name: object,
        destination_name: object,
        **kwargs: object,
    ) -> None:
        nonlocal replaced
        real_rename(source_name, destination_name, **kwargs)  # type: ignore[arg-type]
        if replaced or Path(source_name).name != stage.name:
            return
        replaced = True
        directory_fd = kwargs.get("src_dir_fd")
        flags = library_service.os.O_WRONLY | library_service.os.O_CREAT | library_service.os.O_EXCL
        fd = library_service.os.open(source_name, flags, 0o600, dir_fd=directory_fd)  # type: ignore[arg-type]
        try:
            library_service.os.write(fd, competitor)
        finally:
            library_service.os.close(fd)

    monkeypatch.setattr(library_service.os, "rename", replace_stage_once)
    database.fail_before_commit = True

    with pytest.raises(ForgeException) as exc_info:
        library.import_book(_source(tmp_path / "book.pdf"), ImportMode.COPY)

    assert exc_info.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert stage.read_bytes() == competitor
    assert not final.exists()
    assert library.list_books() == ()


@pytest.mark.parametrize("stream_type", [FlushFailingStream, CloseFailingStream])
def test_copy_stage_flush_and_close_failures_are_safe_storage_errors(
    tmp_path: Path,
    stream_type: type[io.BytesIO],
) -> None:
    source = _source(tmp_path / "book.pdf")
    stage = tmp_path / "private-stage.tmp"
    target_stream = stream_type()

    def stage_opener(path: Path, mode: str) -> io.BytesIO:
        assert (path, mode) == (stage, "xb")
        return target_stream

    with pytest.raises(ForgeException) as exc_info:
        library_service._copy_source_to_stage(
            source,
            stage,
            limits=ExtractionLimits(max_source_bytes=64),
            stage_opener=stage_opener,
        )

    assert exc_info.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert "/private" not in str(exc_info.value)


def test_source_close_failure_does_not_override_a_safe_read_error(
    tmp_path: Path,
) -> None:
    source = tmp_path / "private-source.pdf"
    stage = tmp_path / "stage.tmp"
    source_stream = ReadAndCloseFailingStream(b"unread")

    def source_opener(path: Path, mode: str) -> io.BytesIO:
        assert (path, mode) == (source, "rb")
        return source_stream

    with pytest.raises(ForgeException) as exc_info:
        library_service._copy_source_to_stage(
            source,
            stage,
            limits=ExtractionLimits(max_source_bytes=64),
            source_opener=source_opener,
        )

    assert exc_info.value.code is ForgeErrorCode.SOURCE_CHANGED
    assert "/private" not in str(exc_info.value)
    assert exc_info.value.__cause__ is source_stream.read_error
