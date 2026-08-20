"""Guarded, recoverable publication inside one explicitly authorized vault."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
import sys
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import uuid4

from cove_book_forge.config import ObsidianOutputConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs.managed import parse_obsidian_manifest, plan_obsidian_update
from cove_book_forge.outputs.obsidian_models import ObsidianBookManifest, RenderedObsidianBook
from cove_book_forge.path_safety import validate_relative_path

_O_DIRECTORY: Final = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK: Final = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS: Final = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
_READ_FLAGS: Final = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
_MAX_MANIFEST_BYTES: Final = 2 * 1024 * 1024
_MAX_MARKDOWN_BYTES: Final = 32 * 1024 * 1024
_MAX_CANDIDATE_COUNT: Final = 10_000
_MAX_MANAGED_CHAPTERS: Final = 5_000
_MAX_MANAGED_CARDS: Final = 10_000
_MAX_TOTAL_TRANSACTION_BYTES: Final = 256 * 1024 * 1024
_READ_CHUNK: Final = 1024 * 1024
_TRANSACTIONS_PATH: Final = ".cove-book-forge/.transactions"
_PUBLIC_OUTPUT_ERRORS: Final = frozenset(
    {
        ForgeErrorCode.OUTPUT_NOT_CONFIGURED,
        ForgeErrorCode.PATH_NOT_ALLOWED,
        ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
        ForgeErrorCode.EXTERNAL_MODIFICATION,
    }
)
_SECURE_PRIMITIVES_AVAILABLE: Final = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and all(
        function in os.supports_dir_fd
        for function in (os.open, os.mkdir, os.stat, os.rename, os.link, os.unlink, os.rmdir)
    )
)

_Identity = tuple[int, int]
_Render = Callable[[ObsidianBookManifest | None], RenderedObsidianBook]


def _error(code: ForgeErrorCode) -> ForgeException:
    messages = {
        ForgeErrorCode.OUTPUT_NOT_CONFIGURED: "output is not configured",
        ForgeErrorCode.PATH_NOT_ALLOWED: "output path is not allowed",
        ForgeErrorCode.OUTPUT_PERMISSION_DENIED: "output location is not writable",
        ForgeErrorCode.EXTERNAL_MODIFICATION: "output changed outside this application",
    }
    return ForgeException(code, messages[code])


def _identity(status: os.stat_result) -> _Identity:
    return status.st_dev, status.st_ino


def _require_primitives() -> None:
    if not _SECURE_PRIMITIVES_AVAILABLE:
        raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)


def _is_broad(path: Path) -> bool:
    try:
        home = Path.home().resolve(strict=True)
        current = Path.cwd().resolve(strict=True)
    except (OSError, RuntimeError):
        raise _error(ForgeErrorCode.PATH_NOT_ALLOWED) from None
    return path == Path(path.anchor) or path in (home, current) or path in current.parents


def _matches_broad_directory(identity: _Identity, anchor: str) -> bool:
    try:
        home = Path.home().resolve(strict=True)
        current = Path.cwd().resolve(strict=True)
        candidates = {
            Path(anchor),
            home,
            current,
            *home.parents,
            *current.parents,
        }
        for candidate in candidates:
            descriptor = _open_absolute_directory(candidate, missing_is_unconfigured=False)
            try:
                if _identity(os.fstat(descriptor)) == identity:
                    return True
            finally:
                os.close(descriptor)
        return False
    except ForgeException:
        raise
    except (OSError, RuntimeError, ValueError):
        raise _error(ForgeErrorCode.PATH_NOT_ALLOWED) from None


def _open_absolute_directory(path: Path, *, missing_is_unconfigured: bool) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            previous = descriptor
            descriptor = child
            os.close(previous)
        if descriptor is None:
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        return descriptor
    except FileNotFoundError:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        code = (
            ForgeErrorCode.OUTPUT_NOT_CONFIGURED
            if missing_is_unconfigured
            else ForgeErrorCode.PATH_NOT_ALLOWED
        )
        raise _error(code) from None
    except ForgeException:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise
    except (OSError, RuntimeError, ValueError):
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise _error(ForgeErrorCode.PATH_NOT_ALLOWED) from None


@dataclass
class _VaultAnchor:
    path: Path
    descriptor: int
    identity: _Identity
    closed: bool = False

    @classmethod
    def capture(cls, config: ObsidianOutputConfig) -> _VaultAnchor:
        if not config.enabled or config.vault_path is None:
            raise _error(ForgeErrorCode.OUTPUT_NOT_CONFIGURED)
        _require_primitives()
        raw = str(config.vault_path)
        if "\x00" in raw:
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        path = config.vault_path
        try:
            if not path.is_absolute() or Path(os.path.abspath(path)) != path or _is_broad(path):
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        except (OSError, RuntimeError, ValueError):
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED) from None
        descriptor = _open_absolute_directory(path, missing_is_unconfigured=True)
        try:
            status = os.fstat(descriptor)
            if status.st_mode & 0o222 == 0:
                raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED)
            if _matches_broad_directory(_identity(status), path.anchor):
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            anchor = cls(path=path, descriptor=descriptor, identity=_identity(status))
            anchor.verify()
            return anchor
        except BaseException:
            with suppress(OSError):
                os.close(descriptor)
            raise

    def verify(self) -> None:
        if self.closed:
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        try:
            if _identity(os.fstat(self.descriptor)) != self.identity:
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            reopened = _open_absolute_directory(self.path, missing_is_unconfigured=False)
            try:
                if _identity(os.fstat(reopened)) != self.identity:
                    raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            finally:
                os.close(reopened)
        except ForgeException:
            raise
        except OSError:
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED) from None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            with suppress(OSError):
                os.close(self.descriptor)

    def __enter__(self) -> _VaultAnchor:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


@dataclass(frozen=True)
class _FileSnapshot:
    exists: bool
    parent_identity: _Identity | None = None
    identity: _Identity | None = None
    size: int | None = None
    digest: str | None = None
    data: bytes | None = None


@dataclass(frozen=True)
class _CreatedDirectory:
    path: str
    identity: _Identity


@dataclass(frozen=True)
class _StagedFile:
    path: str
    name: str
    identity: _Identity
    digest: str


class _BackupStatus(StrEnum):
    INTENT = "intent"
    MOVED = "moved"
    VERIFIED = "verified"
    RESTORED = "restored"
    NO_MOVE = "no_move"
    PROTECTED = "protected"


@dataclass
class _BackupFile:
    path: str
    parent_path: str
    source_name: str
    name: str
    parent_identity: _Identity
    identity: _Identity
    digest: str
    size: int
    status: _BackupStatus = _BackupStatus.INTENT


class _PublishedStatus(StrEnum):
    INTENT = "intent"
    PUBLISHED = "published"
    ABSENT = "absent"
    REMOVED = "removed"
    COMPETITOR = "competitor"


@dataclass
class _PublishedFile:
    path: str
    parent_path: str
    name: str
    parent_identity: _Identity
    identity: _Identity
    digest: str
    size: int
    data: bytes
    status: _PublishedStatus = _PublishedStatus.INTENT
    rollback_name: str | None = None


@dataclass
class _RecoveryEntry:
    original_path: str
    container: str
    moved_name: str
    record_name: str
    record_identity: _Identity
    durable: bool = False


@dataclass
class _Transaction:
    parent_fd: int
    name: str
    fd: int
    stage_fd: int
    backup_fd: int
    created_directories: list[_CreatedDirectory]
    staged: dict[str, _StagedFile] = field(default_factory=dict)
    backups: list[_BackupFile] = field(default_factory=list)
    published: list[_PublishedFile] = field(default_factory=list)
    protected_entries: set[tuple[int, str]] = field(default_factory=set)
    recoveries: dict[tuple[int, str], _RecoveryEntry] = field(default_factory=dict)
    expected_writes: Mapping[str, bytes] = field(default_factory=dict)
    closed: bool = False


@dataclass(frozen=True)
class PublisherReceipt:
    rendered: RenderedObsidianBook
    changed_paths: tuple[str, ...]
    unchanged: bool


def _fsync(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EBADF}:
            raise


def _read_descriptor(descriptor: int, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = os.read(descriptor, min(_READ_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b"".join(chunks)
    if len(data) != size:
        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
    return data


def _rename_noreplace(
    source: str,
    destination: str,
    *,
    source_fd: int,
    destination_fd: int,
) -> bool:
    """Atomically rename only when destination is absent, or fail closed."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    function: object
    flags: int
    if sys.platform == "darwin":
        try:
            function = libc.renameatx_np
        except AttributeError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        flags = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        try:
            function = libc.renameat2
        except AttributeError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        flags = 1  # RENAME_NOREPLACE
    else:
        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
    rename_function = ctypes.cast(
        function,
        ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
            use_errno=True,
        ),
    )
    result = rename_function(
        source_fd,
        source_bytes,
        destination_fd,
        destination_bytes,
        flags,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        return False
    raise OSError(error_number, os.strerror(error_number))


class GuardedPublisher:
    """Publish renderer bytes without following links or clobbering race winners."""

    def __init__(self, config: ObsidianOutputConfig) -> None:
        self._config = config

    def publish(self, render: _Render) -> PublisherReceipt:
        try:
            return self._publish(render)
        except ForgeException as exc:
            if exc.code in _PUBLIC_OUTPUT_ERRORS:
                raise
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        except Exception:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None

    def _publish(self, render: _Render) -> PublisherReceipt:
        anchor = _VaultAnchor.capture(self._config)
        with anchor:
            initial = render(None)
            self._validate_rendered_budget(initial)
            initial_book_key = initial.manifest.book_key
            initial_paths = tuple(initial.files)
            manifest_path = self._manifest_path(initial_book_key)
            manifest_snapshot = self._read_snapshot(anchor, manifest_path)
            previous = self._previous_manifest(manifest_snapshot)
            del manifest_snapshot
            if previous is not None:
                self._validate_manifest_budget(previous)
            rendered = render(previous)
            self._validate_rendered_budget(rendered)
            if rendered.manifest.book_key != initial_book_key:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            candidates = self._candidate_paths(initial_paths, rendered, previous)
            del initial
            existing_size = sum(self._candidate_size(anchor, path) for path in candidates)
            staged_size = sum(len(payload) for payload in rendered.files.values())
            if existing_size + staged_size > _MAX_TOTAL_TRANSACTION_BYTES:
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            existing: dict[str, bytes] = {}
            actual_existing_size = 0
            for path in candidates:
                remaining = _MAX_TOTAL_TRANSACTION_BYTES - staged_size - actual_existing_size
                snapshot = self._read_snapshot(anchor, path, max_bytes=remaining)
                if snapshot.exists:
                    assert snapshot.data is not None
                    actual_existing_size += len(snapshot.data)
                    if actual_existing_size + staged_size > _MAX_TOTAL_TRANSACTION_BYTES:
                        raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
                    existing[path] = snapshot.data
            try:
                plan = plan_obsidian_update(previous, existing, rendered)
            except ForgeException as exc:
                if exc.code is ForgeErrorCode.CONFIG_INVALID:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
                raise
            if plan.unchanged:
                anchor.verify()
                return PublisherReceipt(rendered=rendered, changed_paths=(), unchanged=True)

            created: list[_CreatedDirectory] = []
            transaction: _Transaction | None = None
            committed = False
            try:
                for path in sorted({*plan.writes, *plan.removals}):
                    parent = path.rsplit("/", 1)[0] if "/" in path else ""
                    if parent:
                        parent_fd = self._open_directory(
                            anchor, parent, create=True, created=created
                        )
                        assert parent_fd is not None
                        os.close(parent_fd)
                transaction = self._new_transaction(anchor, created)
                self._stage(transaction, plan.writes)
                snapshots: dict[str, _FileSnapshot] = {}
                retained_size = actual_existing_size
                processed_size = 0
                missing = object()
                for path in candidates:
                    expected_bytes = existing.pop(path, missing)
                    expected_exists = isinstance(expected_bytes, bytes)
                    expected_size: int | None = None
                    expected_digest: str | None = None
                    if expected_exists:
                        assert isinstance(expected_bytes, bytes)
                        expected_size = len(expected_bytes)
                        expected_digest = hashlib.sha256(expected_bytes).hexdigest()
                        retained_size -= expected_size
                    del expected_bytes
                    remaining = (
                        _MAX_TOTAL_TRANSACTION_BYTES - staged_size - retained_size - processed_size
                    )
                    snapshot = self._read_snapshot(anchor, path, max_bytes=remaining)
                    if snapshot.size is not None:
                        processed_size += snapshot.size
                    if snapshot.exists != expected_exists:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    if snapshot.exists and (
                        snapshot.size != expected_size or snapshot.digest != expected_digest
                    ):
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    snapshots[path] = _FileSnapshot(
                        exists=snapshot.exists,
                        parent_identity=snapshot.parent_identity,
                        identity=snapshot.identity,
                        size=snapshot.size,
                        digest=snapshot.digest,
                    )
                    del snapshot
                anchor.verify()
                self._commit(
                    anchor,
                    transaction,
                    snapshots,
                    tuple(plan.removals),
                    tuple(plan.writes),
                    manifest_path,
                )
                anchor.verify()
                self._verify_published_files(anchor, transaction)
                anchor.verify()
                committed = True
                self._cleanup_committed(transaction)
                self._close_transaction(transaction)
                self._cleanup_created(anchor, created)
                return PublisherReceipt(
                    rendered=rendered,
                    changed_paths=plan.changed_paths,
                    unchanged=False,
                )
            except BaseException as exc:
                if committed:
                    assert transaction is not None
                    cleanup_signal = self._finish_committed_cleanup(
                        anchor,
                        transaction,
                        created,
                        exc if not isinstance(exc, Exception) else None,
                    )
                    if cleanup_signal is not None:
                        raise cleanup_signal from None
                    return PublisherReceipt(
                        rendered=rendered,
                        changed_paths=plan.changed_paths,
                        unchanged=False,
                    )
                rollback_safe = True
                pending: BaseException = exc
                if transaction is not None:
                    try:
                        rollback_safe = self._rollback(anchor, transaction)
                        if transaction.protected_entries:
                            rollback_safe = False
                    except BaseException as rollback_exc:
                        rollback_safe = False
                        if isinstance(exc, Exception):
                            pending = rollback_exc
                    try:
                        self._cleanup_failed(transaction)
                    except BaseException as cleanup_exc:
                        rollback_safe = False
                        if not isinstance(cleanup_exc, Exception) and isinstance(
                            pending, Exception
                        ):
                            pending = cleanup_exc
                    try:
                        self._close_transaction(transaction)
                    except BaseException as cleanup_exc:
                        rollback_safe = False
                        if not isinstance(cleanup_exc, Exception) and isinstance(
                            pending, Exception
                        ):
                            pending = cleanup_exc
                try:
                    self._cleanup_created(anchor, created)
                except BaseException as cleanup_exc:
                    rollback_safe = False
                    if not isinstance(cleanup_exc, Exception) and isinstance(pending, Exception):
                        pending = cleanup_exc
                if not isinstance(pending, Exception):
                    raise pending from None
                if isinstance(exc, ForgeException):
                    raise
                code = (
                    ForgeErrorCode.OUTPUT_PERMISSION_DENIED
                    if rollback_safe
                    else ForgeErrorCode.EXTERNAL_MODIFICATION
                )
                raise _error(code) from None

    def _finish_committed_cleanup(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        created: list[_CreatedDirectory],
        pending_signal: BaseException | None,
    ) -> BaseException | None:
        cleanups: tuple[Callable[[], None], ...] = (
            lambda: self._cleanup_committed(transaction),
            lambda: self._close_transaction(transaction),
            lambda: self._cleanup_created(anchor, created),
        )
        for cleanup in cleanups:
            for _attempt in range(2):
                try:
                    cleanup()
                except BaseException as cleanup_exc:
                    if not isinstance(cleanup_exc, Exception) and pending_signal is None:
                        pending_signal = cleanup_exc
                    continue
                break
        return pending_signal

    @staticmethod
    def _manifest_path(book_key: str) -> str:
        return validate_relative_path(f".cove-book-forge/obsidian/{book_key}.json")

    def _previous_manifest(self, snapshot: _FileSnapshot) -> ObsidianBookManifest | None:
        if not snapshot.exists:
            return None
        assert snapshot.data is not None
        try:
            return parse_obsidian_manifest(snapshot.data)
        except ForgeException:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None

    def _candidate_paths(
        self,
        initial_paths: tuple[str, ...],
        rendered: RenderedObsidianBook,
        previous: ObsidianBookManifest | None,
    ) -> tuple[str, ...]:
        paths = {*initial_paths, *rendered.files}
        if previous is not None:
            paths.add(previous.moc_path)
            paths.update(chapter.note_path for chapter in previous.chapters)
            paths.update(card.path for card in previous.cards)
            paths.add(self._manifest_path(previous.book_key))
        try:
            validated = tuple(sorted(validate_relative_path(path) for path in paths))
        except (TypeError, ValueError):
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED) from None
        if len(validated) > _MAX_CANDIDATE_COUNT:
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        return validated

    @staticmethod
    def _validate_manifest_budget(manifest: ObsidianBookManifest) -> None:
        if (
            len(manifest.chapters) > _MAX_MANAGED_CHAPTERS
            or len(manifest.cards) > _MAX_MANAGED_CARDS
        ):
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)

    def _validate_rendered_budget(self, rendered: RenderedObsidianBook) -> None:
        self._validate_manifest_budget(rendered.manifest)
        if len(rendered.files) > _MAX_CANDIDATE_COUNT:
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        total = 0
        for payload in rendered.files.values():
            if not isinstance(payload, bytes):
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            total += len(payload)
            if total > _MAX_TOTAL_TRANSACTION_BYTES:
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)

    def _candidate_size(self, anchor: _VaultAnchor, path: str) -> int:
        anchor.verify()
        path = validate_relative_path(path)
        parent_path, _, name = path.rpartition("/")
        parent = self._open_directory(anchor, parent_path, create=False)
        if parent is None:
            return 0
        try:
            try:
                status = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return 0
            if not stat.S_ISREG(status.st_mode):
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            limit = _MAX_MANIFEST_BYTES if path.endswith(".json") else _MAX_MARKDOWN_BYTES
            if status.st_size > limit:
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            return status.st_size
        except ForgeException:
            raise
        except PermissionError:
            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
        except OSError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        finally:
            os.close(parent)

    def _open_directory(
        self,
        anchor: _VaultAnchor,
        relative: str,
        *,
        create: bool,
        created: list[_CreatedDirectory] | None = None,
    ) -> int | None:
        if not relative:
            return os.dup(anchor.descriptor)
        validate_relative_path(relative)
        parent = os.dup(anchor.descriptor)
        walked: list[str] = []
        try:
            for component in relative.split("/"):
                walked.append(component)
                child: int | None = None
                try:
                    try:
                        child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
                    except FileNotFoundError:
                        if not create:
                            os.close(parent)
                            return None
                        try:
                            os.mkdir(component, mode=0o700, dir_fd=parent)
                            created_status = os.stat(
                                component,
                                dir_fd=parent,
                                follow_symlinks=False,
                            )
                            if not stat.S_ISDIR(created_status.st_mode):
                                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
                            if created is not None:
                                created.append(
                                    _CreatedDirectory("/".join(walked), _identity(created_status))
                                )
                            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
                        except OSError:
                            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
                    except OSError:
                        raise _error(ForgeErrorCode.PATH_NOT_ALLOWED) from None
                    entry = os.stat(component, dir_fd=parent, follow_symlinks=False)
                    if not stat.S_ISDIR(entry.st_mode) or _identity(entry) != _identity(
                        os.fstat(child)
                    ):
                        raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
                    previous = parent
                    parent = child
                    child = None
                    os.close(previous)
                finally:
                    if child is not None:
                        with suppress(OSError):
                            os.close(child)
            return parent
        except BaseException:
            with suppress(OSError):
                os.close(parent)
            raise

    def _read_snapshot(
        self,
        anchor: _VaultAnchor,
        path: str,
        *,
        max_bytes: int | None = None,
    ) -> _FileSnapshot:
        anchor.verify()
        path = validate_relative_path(path)
        parent_path, _, name = path.rpartition("/")
        parent = self._open_directory(anchor, parent_path, create=False)
        if parent is None:
            return _FileSnapshot(False)
        descriptor: int | None = None
        try:
            parent_identity = _identity(os.fstat(parent))
            try:
                entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return _FileSnapshot(False, parent_identity=parent_identity)
            if not stat.S_ISREG(entry.st_mode):
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            limit = _MAX_MANIFEST_BYTES if path.endswith(".json") else _MAX_MARKDOWN_BYTES
            allowed = limit if max_bytes is None else min(limit, max(0, max_bytes))
            if entry.st_size > allowed:
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            descriptor = os.open(name, _READ_FLAGS, dir_fd=parent)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > allowed:
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            data = _read_descriptor(descriptor, before.st_size)
            after = os.fstat(descriptor)
            if _identity(before) != _identity(entry) or _identity(after) != _identity(before):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if after.st_size != len(data):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            return _FileSnapshot(
                True,
                parent_identity=parent_identity,
                identity=_identity(after),
                size=after.st_size,
                digest=hashlib.sha256(data).hexdigest(),
                data=data,
            )
        except ForgeException:
            raise
        except PermissionError:
            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
        except OSError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            with suppress(OSError):
                os.close(parent)

    def _new_transaction(
        self, anchor: _VaultAnchor, created: list[_CreatedDirectory]
    ) -> _Transaction:
        parent_fd = self._open_directory(anchor, _TRANSACTIONS_PATH, create=True, created=created)
        assert parent_fd is not None
        name = f"tx-{uuid4().hex}"
        tx_fd: int | None = None
        stage_fd: int | None = None
        backup_fd: int | None = None
        tx_identity: _Identity | None = None
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            created_status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(created_status.st_mode):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            tx_identity = _identity(created_status)
            tx_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            if _identity(os.fstat(tx_fd)) != tx_identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            os.mkdir("stage", mode=0o700, dir_fd=tx_fd)
            os.mkdir("backup", mode=0o700, dir_fd=tx_fd)
            stage_fd = os.open("stage", _DIRECTORY_FLAGS, dir_fd=tx_fd)
            backup_fd = os.open("backup", _DIRECTORY_FLAGS, dir_fd=tx_fd)
            _fsync(parent_fd)
            return _Transaction(parent_fd, name, tx_fd, stage_fd, backup_fd, created)
        except BaseException as exc:
            for descriptor in (backup_fd, stage_fd):
                if descriptor is not None:
                    with suppress(OSError):
                        os.close(descriptor)
            if tx_fd is not None:
                with suppress(OSError):
                    os.rmdir("backup", dir_fd=tx_fd)
                with suppress(OSError):
                    os.rmdir("stage", dir_fd=tx_fd)
                with suppress(OSError):
                    os.close(tx_fd)
            if tx_identity is not None:
                try:
                    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if stat.S_ISDIR(current.st_mode) and _identity(current) == tx_identity:
                        os.rmdir(name, dir_fd=parent_fd)
                        _fsync(parent_fd)
                except (FileNotFoundError, OSError):
                    pass
            with suppress(OSError):
                os.close(parent_fd)
            if isinstance(exc, OSError):
                raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
            raise

    def _stage(self, transaction: _Transaction, writes: Mapping[str, bytes]) -> None:
        transaction.expected_writes = dict(writes)
        for index, (path, data) in enumerate(writes.items()):
            if not isinstance(path, str) or not isinstance(data, bytes):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if len(data) > (_MAX_MANIFEST_BYTES if path.endswith(".json") else _MAX_MARKDOWN_BYTES):
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            name = f"s{index:06d}"
            descriptor: int | None = None
            try:
                descriptor = os.open(name, _WRITE_FLAGS, 0o600, dir_fd=transaction.stage_fd)
                opened_status = os.fstat(descriptor)
                transaction.staged[path] = _StagedFile(path, name, _identity(opened_status), "")
                offset = 0
                while offset < len(data):
                    written = os.write(descriptor, data[offset:])
                    if written <= 0:
                        raise OSError("short write")
                    offset += written
                _fsync(descriptor)
                status = os.fstat(descriptor)
                verified = self._snapshot_in_directory(transaction.stage_fd, name)
                expected_digest = hashlib.sha256(data).hexdigest()
                if (
                    verified.identity != _identity(status)
                    or verified.digest != expected_digest
                    or verified.data != data
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                transaction.staged[path] = _StagedFile(
                    path, name, _identity(status), expected_digest
                )
            finally:
                if descriptor is not None:
                    with suppress(OSError):
                        os.close(descriptor)
        _fsync(transaction.stage_fd)

    def _require_snapshot(self, anchor: _VaultAnchor, path: str, expected: _FileSnapshot) -> None:
        current = self._read_snapshot(anchor, path)
        if (
            current.exists != expected.exists
            or current.parent_identity != expected.parent_identity
            or current.identity != expected.identity
            or current.size != expected.size
            or current.digest != expected.digest
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _commit(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        snapshots: dict[str, _FileSnapshot],
        removals: tuple[str, ...],
        writes: tuple[str, ...],
        manifest_path: str,
    ) -> None:
        for path in sorted(removals):
            anchor.verify()
            self._backup(anchor, transaction, path, snapshots[path])
        ordered = [path for path in sorted(writes) if path != manifest_path]
        if manifest_path in writes:
            ordered.append(manifest_path)
        for path in ordered:
            anchor.verify()
            expected = snapshots[path]
            if expected.exists and not any(item.path == path for item in transaction.backups):
                self._backup(anchor, transaction, path, expected)
            if path == manifest_path:
                self._verify_published_files(anchor, transaction)
            self._publish_stage(anchor, transaction, path, expected)

    def _backup(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        path: str,
        expected: _FileSnapshot,
    ) -> None:
        if (
            not expected.exists
            or expected.parent_identity is None
            or expected.identity is None
            or expected.size is None
            or expected.digest is None
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        self._require_snapshot(anchor, path, expected)
        parent_path, _, name = path.rpartition("/")
        parent = self._open_directory(anchor, parent_path, create=False)
        if parent is None or _identity(os.fstat(parent)) != expected.parent_identity:
            if parent is not None:
                os.close(parent)
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        backup_name = f"b{len(transaction.backups):06d}"
        backup = _BackupFile(
            path=path,
            parent_path=parent_path,
            source_name=name,
            name=backup_name,
            parent_identity=expected.parent_identity,
            identity=expected.identity,
            digest=expected.digest,
            size=expected.size,
        )
        transaction.backups.append(backup)
        try:
            self._protect_moved(
                transaction,
                transaction.backup_fd,
                "backup",
                backup_name,
                path,
            )
            moved = _rename_noreplace(
                name,
                backup_name,
                source_fd=parent,
                destination_fd=transaction.backup_fd,
            )
            if not moved:
                backup.status = _BackupStatus.PROTECTED
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            backup.status = _BackupStatus.MOVED
            moved_snapshot = self._snapshot_in_directory(transaction.backup_fd, backup_name)
            if (
                moved_snapshot.identity != backup.identity
                or moved_snapshot.size != backup.size
                or moved_snapshot.digest != backup.digest
            ):
                backup.status = _BackupStatus.PROTECTED
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            backup.status = _BackupStatus.VERIFIED
            _fsync(parent)
            _fsync(transaction.backup_fd)
        except FileNotFoundError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        except ForgeException:
            raise
        except PermissionError:
            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
        except OSError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        finally:
            with suppress(OSError):
                os.close(parent)

    @staticmethod
    def _entry_exists(directory_fd: int, name: str) -> bool:
        try:
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def _protect_moved(
        self,
        transaction: _Transaction,
        directory_fd: int,
        container: str,
        moved_name: str,
        original_path: str,
    ) -> None:
        key = (directory_fd, moved_name)
        existing = transaction.recoveries.get(key)
        if existing is not None:
            if existing.durable:
                return
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        transaction.protected_entries.add(key)
        recovery_id = uuid4().hex
        record_name = f"recovery-{recovery_id}.json"
        payload = json.dumps(
            {
                "container": container,
                "moved_name": moved_name,
                "original_path": original_path,
                "recovery_id": recovery_id,
                "schema": 1,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        descriptor: int | None = None
        try:
            descriptor = os.open(record_name, _WRITE_FLAGS, 0o600, dir_fd=transaction.fd)
            identity = _identity(os.fstat(descriptor))
            recovery = _RecoveryEntry(
                original_path,
                container,
                moved_name,
                record_name,
                identity,
            )
            transaction.recoveries[key] = recovery
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short recovery write")
                offset += written
            _fsync(descriptor)
            _fsync(transaction.fd)
            recovery.durable = True
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

    def _forget_moved(self, transaction: _Transaction, directory_fd: int, moved_name: str) -> None:
        key = (directory_fd, moved_name)
        recovery = transaction.recoveries.get(key)
        if recovery is not None:
            removed = self._unlink_if_identity(
                transaction,
                transaction.fd,
                recovery.record_name,
                recovery.record_identity,
            )
            if not removed:
                return
            transaction.recoveries.pop(key, None)
        transaction.protected_entries.discard(key)

    def _restore_moved(
        self,
        transaction: _Transaction,
        directory_fd: int,
        moved_name: str,
        parent_fd: int,
        target_name: str,
    ) -> bool:
        try:
            restored = _rename_noreplace(
                moved_name,
                target_name,
                source_fd=directory_fd,
                destination_fd=parent_fd,
            )
        except (ForgeException, OSError):
            return False
        if not restored:
            return False
        self._forget_moved(transaction, directory_fd, moved_name)
        _fsync(directory_fd)
        _fsync(parent_fd)
        return True

    def _snapshot_in_directory(self, directory_fd: int, name: str) -> _FileSnapshot:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_MARKDOWN_BYTES:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            data = _read_descriptor(descriptor, before.st_size)
            after = os.fstat(descriptor)
            if (
                _identity(before) != _identity(after)
                or after.st_size != before.st_size
                or after.st_size != len(data)
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            return _FileSnapshot(
                True,
                identity=_identity(after),
                size=after.st_size,
                digest=hashlib.sha256(data).hexdigest(),
                data=data,
            )
        finally:
            os.close(descriptor)

    def _publish_stage(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        path: str,
        expected: _FileSnapshot,
    ) -> None:
        staged = transaction.staged[path]
        expected_bytes = transaction.expected_writes[path]
        staged_now = self._snapshot_in_directory(transaction.stage_fd, staged.name)
        if (
            staged_now.identity != staged.identity
            or staged_now.digest != staged.digest
            or staged_now.data != expected_bytes
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        parent_path, _, name = path.rpartition("/")
        parent = self._open_directory(anchor, parent_path, create=False)
        if parent is None:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        try:
            parent_identity = _identity(os.fstat(parent))
            if expected.parent_identity != parent_identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            published = _PublishedFile(
                path=path,
                parent_path=parent_path,
                name=name,
                parent_identity=parent_identity,
                identity=staged.identity,
                digest=staged.digest,
                size=len(expected_bytes),
                data=expected_bytes,
            )
            transaction.published.append(published)
            os.link(
                staged.name,
                name,
                src_dir_fd=transaction.stage_fd,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            status = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if _identity(status) != staged.identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            target = self._snapshot_in_directory(parent, name)
            if (
                target.identity != staged.identity
                or target.digest != staged.digest
                or target.data != expected_bytes
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            published.status = _PublishedStatus.PUBLISHED
            stage_status = os.stat(staged.name, dir_fd=transaction.stage_fd, follow_symlinks=False)
            if _identity(stage_status) != staged.identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            os.unlink(staged.name, dir_fd=transaction.stage_fd)
            _fsync(transaction.stage_fd)
            _fsync(parent)
        except FileExistsError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        except ForgeException:
            raise
        except PermissionError:
            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
        except OSError:
            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
        finally:
            with suppress(OSError):
                os.close(parent)

    def _verify_published_files(self, anchor: _VaultAnchor, transaction: _Transaction) -> None:
        for published in transaction.published:
            expected = published.data
            current = self._read_snapshot(anchor, published.path)
            if (
                not current.exists
                or current.parent_identity != published.parent_identity
                or current.identity != published.identity
                or current.digest != published.digest
                or current.data != expected
                or len(expected) != published.size
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _rollback(self, anchor: _VaultAnchor, transaction: _Transaction) -> bool:
        safe = True
        pending_signal: BaseException | None = None
        for published in reversed(transaction.published):
            removed = False
            for _attempt in range(3):
                try:
                    removed = self._remove_published(anchor, transaction, published)
                except BaseException as exc:
                    if not isinstance(exc, Exception) and pending_signal is None:
                        pending_signal = exc
                    continue
                break
            if not removed:
                published.status = _PublishedStatus.COMPETITOR
                safe = False
        for backup in reversed(transaction.backups):
            restored = False
            for _attempt in range(3):
                try:
                    restored = self._reconcile_backup(anchor, transaction, backup)
                except BaseException as exc:
                    if not isinstance(exc, Exception) and pending_signal is None:
                        pending_signal = exc
                    continue
                break
            if not restored:
                safe = False
                backup.status = _BackupStatus.PROTECTED
        if pending_signal is not None:
            raise pending_signal
        return safe

    def _reconcile_backup(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        backup: _BackupFile,
    ) -> bool:
        visible: _FileSnapshot | None = None
        with suppress(ForgeException, OSError):
            visible = self._read_snapshot(anchor, backup.path)
        destination: _FileSnapshot | None = None
        destination_exists = False
        try:
            destination = self._snapshot_in_directory(transaction.backup_fd, backup.name)
            destination_exists = True
        except FileNotFoundError:
            pass
        except (ForgeException, OSError):
            destination_exists = self._entry_exists(transaction.backup_fd, backup.name)
        if visible is None:
            if not destination_exists:
                backup.status = _BackupStatus.NO_MOVE
                self._forget_moved(transaction, transaction.backup_fd, backup.name)
            else:
                backup.status = _BackupStatus.PROTECTED
            return False
        visible_is_old = self._snapshot_matches_backup(visible, backup)
        destination_is_old = destination is not None and self._snapshot_matches_backup(
            destination, backup
        )
        if visible_is_old:
            if destination is None:
                backup.status = _BackupStatus.NO_MOVE
            elif destination_is_old:
                backup.status = _BackupStatus.RESTORED
            else:
                backup.status = _BackupStatus.PROTECTED
                return False
            self._forget_moved(transaction, transaction.backup_fd, backup.name)
            return True
        if visible.exists or not destination_is_old:
            if not visible.exists and destination_exists:
                self._restore_unverified_backup_entry(anchor, transaction, backup)
            backup.status = _BackupStatus.PROTECTED
            return False
        return self._restore_backup(anchor, transaction, backup)

    def _restore_unverified_backup_entry(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        backup: _BackupFile,
    ) -> bool:
        parent = self._open_directory(anchor, backup.parent_path, create=False)
        if parent is None:
            return False
        try:
            if _identity(os.fstat(parent)) != backup.parent_identity:
                return False
            return self._restore_moved(
                transaction,
                transaction.backup_fd,
                backup.name,
                parent,
                backup.source_name,
            )
        finally:
            os.close(parent)

    @staticmethod
    def _snapshot_matches_backup(snapshot: _FileSnapshot, backup: _BackupFile) -> bool:
        return (
            snapshot.exists
            and snapshot.parent_identity in {None, backup.parent_identity}
            and snapshot.identity == backup.identity
            and snapshot.size == backup.size
            and snapshot.digest == backup.digest
        )

    def _remove_published(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        published: _PublishedFile,
    ) -> bool:
        parent_path = published.parent_path
        name = published.name
        parent = self._open_directory(anchor, parent_path, create=False)
        if parent is None or _identity(os.fstat(parent)) != published.parent_identity:
            if parent is not None:
                os.close(parent)
            return False
        try:
            if published.rollback_name is not None:
                settled = self._settle_published_move(
                    transaction,
                    published,
                    parent,
                )
                if settled is not None:
                    return settled
            rollback_name = f"r{len(transaction.protected_entries):06d}-{uuid4().hex}"
            published.rollback_name = rollback_name
            self._protect_moved(
                transaction,
                transaction.fd,
                "transaction",
                rollback_name,
                published.path,
            )
            try:
                os.rename(
                    name,
                    rollback_name,
                    src_dir_fd=parent,
                    dst_dir_fd=transaction.fd,
                )
            except BaseException as rename_exc:
                settled = self._settle_published_move(transaction, published, parent)
                if settled:
                    if not isinstance(rename_exc, Exception):
                        raise rename_exc from None
                    return True
                raise
            settled = self._settle_published_move(transaction, published, parent)
            return bool(settled)
        except FileNotFoundError:
            if published.rollback_name is not None:
                self._forget_moved(transaction, transaction.fd, published.rollback_name)
                published.rollback_name = None
            published.status = _PublishedStatus.ABSENT
            return True
        finally:
            os.close(parent)

    def _settle_published_move(
        self,
        transaction: _Transaction,
        published: _PublishedFile,
        parent: int,
    ) -> bool | None:
        rollback_name = published.rollback_name
        if rollback_name is None:
            return None
        if not self._entry_exists(transaction.fd, rollback_name):
            self._forget_moved(transaction, transaction.fd, rollback_name)
            published.rollback_name = None
            return None
        removed = self._discard_moved_published(transaction, rollback_name, published)
        if removed:
            _fsync(transaction.fd)
            _fsync(parent)
            published.status = _PublishedStatus.REMOVED
            published.rollback_name = None
            return True
        restored = self._restore_moved(
            transaction,
            transaction.fd,
            rollback_name,
            parent,
            published.name,
        )
        if restored:
            published.rollback_name = None
        published.status = _PublishedStatus.COMPETITOR
        return False

    def _discard_moved_published(
        self,
        transaction: _Transaction,
        rollback_name: str,
        published: _PublishedFile,
    ) -> bool:
        self._protect_moved(
            transaction,
            transaction.fd,
            "transaction",
            rollback_name,
            published.path,
        )
        moved_status = os.stat(
            rollback_name,
            dir_fd=transaction.fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(moved_status.st_mode):
            return False
        moved = self._snapshot_in_directory(transaction.fd, rollback_name)
        expected = published.data
        if (
            moved.identity != published.identity
            or moved.digest != published.digest
            or moved.data != expected
            or len(expected) != published.size
        ):
            return False
        os.unlink(rollback_name, dir_fd=transaction.fd)
        self._forget_moved(transaction, transaction.fd, rollback_name)
        return True

    def _restore_backup(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        backup: _BackupFile,
    ) -> bool:
        parent_path = backup.parent_path
        name = backup.source_name
        try:
            stored = self._snapshot_in_directory(transaction.backup_fd, backup.name)
            if (
                stored.identity != backup.identity
                or stored.digest != backup.digest
                or stored.size != backup.size
            ):
                return False
            parent = self._open_directory(anchor, parent_path, create=False)
            if parent is None or _identity(os.fstat(parent)) != backup.parent_identity:
                if parent is not None:
                    os.close(parent)
                return False
            try:
                os.link(
                    backup.name,
                    name,
                    src_dir_fd=transaction.backup_fd,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
                try:
                    visible = self._snapshot_in_directory(parent, name)
                except BaseException:
                    self._unlink_named_identity(parent, name, backup.identity)
                    raise
                restored = (
                    visible.identity == backup.identity
                    and visible.digest == backup.digest
                    and visible.size == backup.size
                )
                if not restored:
                    self._unlink_named_identity(parent, name, backup.identity)
                    return False
                backup.status = _BackupStatus.RESTORED
                self._forget_moved(transaction, transaction.backup_fd, backup.name)
                _fsync(parent)
                return True
            except FileExistsError:
                return False
            finally:
                os.close(parent)
        except (ForgeException, OSError):
            return False

    @staticmethod
    def _unlink_named_identity(directory_fd: int, name: str, expected: _Identity) -> None:
        try:
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISREG(status.st_mode) and _identity(status) == expected:
                os.unlink(name, dir_fd=directory_fd)
        except (FileNotFoundError, OSError):
            pass

    def _cleanup_committed(self, transaction: _Transaction) -> None:
        for staged in transaction.staged.values():
            self._unlink_if_identity(
                transaction, transaction.stage_fd, staged.name, staged.identity
            )
        for backup in transaction.backups:
            removed = self._unlink_identity_without_protection(
                transaction.backup_fd,
                backup.name,
                backup.identity,
            )
            if removed:
                self._forget_moved(transaction, transaction.backup_fd, backup.name)
        _fsync(transaction.stage_fd)
        _fsync(transaction.backup_fd)

    def _cleanup_failed(self, transaction: _Transaction) -> None:
        with suppress(OSError, ForgeException):
            for staged in transaction.staged.values():
                self._unlink_if_identity(
                    transaction, transaction.stage_fd, staged.name, staged.identity
                )
            for backup in transaction.backups:
                if backup.status in {_BackupStatus.RESTORED, _BackupStatus.NO_MOVE}:
                    self._unlink_if_identity(
                        transaction,
                        transaction.backup_fd,
                        backup.name,
                        backup.identity,
                    )
            _fsync(transaction.stage_fd)
            _fsync(transaction.backup_fd)

    @staticmethod
    def _unlink_identity_without_protection(
        directory_fd: int,
        name: str,
        expected: _Identity,
    ) -> bool:
        try:
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        if stat.S_ISREG(status.st_mode) and _identity(status) == expected:
            os.unlink(name, dir_fd=directory_fd)
            return True
        return False

    @staticmethod
    def _unlink_if_identity(
        transaction: _Transaction, directory_fd: int, name: str, expected: _Identity
    ) -> bool:
        if (directory_fd, name) in transaction.protected_entries:
            return False
        try:
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return True
        if stat.S_ISREG(status.st_mode) and _identity(status) == expected:
            os.unlink(name, dir_fd=directory_fd)
            return True
        return False

    def _close_transaction(self, transaction: _Transaction) -> None:
        if transaction.closed:
            return
        pending_signal: BaseException | None = None
        actions: tuple[Callable[[], None], ...] = (
            lambda: os.close(transaction.stage_fd),
            lambda: os.close(transaction.backup_fd),
            lambda: os.rmdir("stage", dir_fd=transaction.fd),
            lambda: os.rmdir("backup", dir_fd=transaction.fd),
            lambda: os.close(transaction.fd),
            lambda: os.rmdir(transaction.name, dir_fd=transaction.parent_fd),
            lambda: os.close(transaction.parent_fd),
        )
        for action in actions:
            for _attempt in range(2):
                try:
                    action()
                except OSError:
                    break
                except BaseException as exc:
                    if pending_signal is None:
                        pending_signal = exc
                    continue
                break
        transaction.closed = True
        if pending_signal is not None:
            raise pending_signal

    def _cleanup_created(self, anchor: _VaultAnchor, created: Iterable[_CreatedDirectory]) -> None:
        pending_signal: BaseException | None = None
        for item in reversed(tuple(created)):
            parent_path, _, name = item.path.rpartition("/")
            try:
                parent = self._open_directory(anchor, parent_path, create=False)
                if parent is None:
                    continue
                try:
                    status = os.stat(name, dir_fd=parent, follow_symlinks=False)
                    if stat.S_ISDIR(status.st_mode) and _identity(status) == item.identity:
                        os.rmdir(name, dir_fd=parent)
                finally:
                    os.close(parent)
            except (OSError, ForgeException):
                continue
            except BaseException as exc:
                if pending_signal is None:
                    pending_signal = exc
                continue
        if pending_signal is not None:
            raise pending_signal
