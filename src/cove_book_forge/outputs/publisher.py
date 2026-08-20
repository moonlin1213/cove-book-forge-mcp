"""Guarded, recoverable publication inside one explicitly authorized vault."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
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
_DIRECTORY_FLAGS: Final = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
_READ_FLAGS: Final = os.O_RDONLY | _O_NOFOLLOW
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
_MAX_MANIFEST_BYTES: Final = 2 * 1024 * 1024
_MAX_MARKDOWN_BYTES: Final = 32 * 1024 * 1024
_READ_CHUNK: Final = 1024 * 1024
_TRANSACTIONS_PATH: Final = ".cove-book-forge/.transactions"
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


def _open_absolute_directory(path: Path, *, missing_is_unconfigured: bool) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
        for component in path.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
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


@dataclass(frozen=True)
class _BackupFile:
    path: str
    name: str
    parent_identity: _Identity
    identity: _Identity
    digest: str


@dataclass(frozen=True)
class _PublishedFile:
    path: str
    parent_identity: _Identity
    identity: _Identity
    digest: str


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


class GuardedPublisher:
    """Publish renderer bytes without following links or clobbering race winners."""

    def __init__(self, config: ObsidianOutputConfig) -> None:
        self._config = config

    def publish(self, render: _Render) -> PublisherReceipt:
        anchor = _VaultAnchor.capture(self._config)
        with anchor:
            initial = render(None)
            manifest_path = self._manifest_path(initial.manifest.book_key)
            manifest_snapshot = self._read_snapshot(anchor, manifest_path)
            previous = self._previous_manifest(manifest_snapshot)
            rendered = render(previous)
            if rendered.manifest.book_key != initial.manifest.book_key:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            candidates = self._candidate_paths(initial, rendered, previous)
            existing: dict[str, bytes] = {}
            for path in candidates:
                snapshot = self._read_snapshot(anchor, path)
                if snapshot.exists:
                    assert snapshot.data is not None
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
                snapshots = {path: self._read_snapshot(anchor, path) for path in candidates}
                self._match_planning_bytes(existing, snapshots)
                anchor.verify()
                for path, expected in snapshots.items():
                    self._require_snapshot(anchor, path, expected)
                self._commit(
                    anchor,
                    transaction,
                    snapshots,
                    tuple(plan.removals),
                    tuple(plan.writes),
                    manifest_path,
                )
                anchor.verify()
                self._cleanup_committed(transaction)
                self._close_transaction(transaction)
                self._cleanup_created(anchor, created)
                return PublisherReceipt(
                    rendered=rendered,
                    changed_paths=plan.changed_paths,
                    unchanged=False,
                )
            except BaseException as exc:
                rollback_safe = True
                if transaction is not None:
                    rollback_safe = self._rollback(anchor, transaction)
                    self._cleanup_failed(transaction)
                    self._close_transaction(transaction)
                self._cleanup_created(anchor, created)
                if isinstance(exc, ForgeException):
                    raise
                code = (
                    ForgeErrorCode.OUTPUT_PERMISSION_DENIED
                    if rollback_safe
                    else ForgeErrorCode.EXTERNAL_MODIFICATION
                )
                raise _error(code) from None

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
        initial: RenderedObsidianBook,
        rendered: RenderedObsidianBook,
        previous: ObsidianBookManifest | None,
    ) -> tuple[str, ...]:
        paths = {*initial.files, *rendered.files}
        if previous is not None:
            paths.add(previous.moc_path)
            paths.update(chapter.note_path for chapter in previous.chapters)
            paths.update(card.path for card in previous.cards)
            paths.add(self._manifest_path(previous.book_key))
        try:
            return tuple(sorted(validate_relative_path(path) for path in paths))
        except (TypeError, ValueError):
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED) from None

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
                try:
                    child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
                except FileNotFoundError:
                    if not create:
                        os.close(parent)
                        return None
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=parent)
                        child = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent)
                    except OSError:
                        raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
                    status = os.fstat(child)
                    if created is not None:
                        created.append(_CreatedDirectory("/".join(walked), _identity(status)))
                except OSError:
                    raise _error(ForgeErrorCode.PATH_NOT_ALLOWED) from None
                entry = os.stat(component, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISDIR(entry.st_mode) or _identity(entry) != _identity(
                    os.fstat(child)
                ):
                    os.close(child)
                    raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
                os.close(parent)
                parent = child
            return parent
        except BaseException:
            with suppress(OSError):
                os.close(parent)
            raise

    def _read_snapshot(self, anchor: _VaultAnchor, path: str) -> _FileSnapshot:
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
            if entry.st_size > limit:
                raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
            descriptor = os.open(name, _READ_FLAGS, dir_fd=parent)
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
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
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            tx_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            os.mkdir("stage", mode=0o700, dir_fd=tx_fd)
            os.mkdir("backup", mode=0o700, dir_fd=tx_fd)
            stage_fd = os.open("stage", _DIRECTORY_FLAGS, dir_fd=tx_fd)
            backup_fd = os.open("backup", _DIRECTORY_FLAGS, dir_fd=tx_fd)
            _fsync(parent_fd)
            return _Transaction(parent_fd, name, tx_fd, stage_fd, backup_fd, created)
        except OSError:
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
                with suppress(OSError):
                    os.rmdir(name, dir_fd=parent_fd)
            with suppress(OSError):
                os.close(parent_fd)
            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None

    def _stage(self, transaction: _Transaction, writes: Mapping[str, bytes]) -> None:
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

    @staticmethod
    def _match_planning_bytes(
        existing: dict[str, bytes], snapshots: dict[str, _FileSnapshot]
    ) -> None:
        for path, snapshot in snapshots.items():
            if snapshot.exists != (path in existing):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if snapshot.exists and snapshot.data != existing[path]:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _require_snapshot(self, anchor: _VaultAnchor, path: str, expected: _FileSnapshot) -> None:
        current = self._read_snapshot(anchor, path)
        if current != expected:
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
        try:
            os.rename(
                name,
                backup_name,
                src_dir_fd=parent,
                dst_dir_fd=transaction.backup_fd,
            )
            moved = self._snapshot_in_directory(transaction.backup_fd, backup_name)
            if moved.identity != expected.identity or moved.digest != expected.digest:
                self._restore_unexpected(
                    transaction, transaction.backup_fd, backup_name, parent, name, moved.identity
                )
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            transaction.backups.append(
                _BackupFile(
                    path,
                    backup_name,
                    expected.parent_identity,
                    expected.identity,
                    expected.digest,
                )
            )
            _fsync(parent)
            _fsync(transaction.backup_fd)
        except FileNotFoundError:
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

    def _snapshot_in_directory(self, directory_fd: int, name: str) -> _FileSnapshot:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_MARKDOWN_BYTES:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            data = _read_descriptor(descriptor, before.st_size)
            after = os.fstat(descriptor)
            if _identity(before) != _identity(after):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            return _FileSnapshot(
                True,
                identity=_identity(after),
                digest=hashlib.sha256(data).hexdigest(),
                data=data,
            )
        finally:
            os.close(descriptor)

    def _restore_unexpected(
        self,
        transaction: _Transaction,
        directory_fd: int,
        source_name: str,
        parent_fd: int,
        target_name: str,
        identity: _Identity | None,
    ) -> None:
        try:
            os.link(
                source_name,
                target_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                identity is not None
                and _identity(os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False))
                == identity
            ):
                os.unlink(source_name, dir_fd=directory_fd)
                return
        except OSError:
            pass
        transaction.protected_entries.add((directory_fd, source_name))

    def _publish_stage(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        path: str,
        expected: _FileSnapshot,
    ) -> None:
        staged = transaction.staged[path]
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
            transaction.published.append(
                _PublishedFile(path, parent_identity, staged.identity, staged.digest)
            )
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

    def _rollback(self, anchor: _VaultAnchor, transaction: _Transaction) -> bool:
        safe = True
        for published in reversed(transaction.published):
            if not self._remove_published(anchor, transaction, published):
                safe = False
        for backup in reversed(transaction.backups):
            if not self._restore_backup(anchor, transaction, backup):
                safe = False
                transaction.protected_entries.add((transaction.backup_fd, backup.name))
        return safe

    def _remove_published(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        published: _PublishedFile,
    ) -> bool:
        parent_path, _, name = published.path.rpartition("/")
        try:
            parent = self._open_directory(anchor, parent_path, create=False)
            if parent is None or _identity(os.fstat(parent)) != published.parent_identity:
                if parent is not None:
                    os.close(parent)
                return False
            rollback_name = f"r{len(transaction.protected_entries):06d}-{uuid4().hex}"
            try:
                os.rename(
                    name,
                    rollback_name,
                    src_dir_fd=parent,
                    dst_dir_fd=transaction.fd,
                )
                moved = self._snapshot_in_directory(transaction.fd, rollback_name)
                if moved.identity == published.identity and moved.digest == published.digest:
                    os.unlink(rollback_name, dir_fd=transaction.fd)
                    return True
                self._restore_unexpected(
                    transaction, transaction.fd, rollback_name, parent, name, moved.identity
                )
                return False
            except FileNotFoundError:
                return False
            finally:
                os.close(parent)
        except (ForgeException, OSError):
            return False

    def _restore_backup(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        backup: _BackupFile,
    ) -> bool:
        parent_path, _, name = backup.path.rpartition("/")
        try:
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
                status = os.stat(name, dir_fd=parent, follow_symlinks=False)
                return _identity(status) == backup.identity
            except FileExistsError:
                return False
            finally:
                os.close(parent)
        except (ForgeException, OSError):
            return False

    def _cleanup_committed(self, transaction: _Transaction) -> None:
        self._cleanup_entries(transaction)

    def _cleanup_failed(self, transaction: _Transaction) -> None:
        with suppress(OSError, ForgeException):
            self._cleanup_entries(transaction)

    def _cleanup_entries(self, transaction: _Transaction) -> None:
        for staged in transaction.staged.values():
            self._unlink_if_identity(
                transaction, transaction.stage_fd, staged.name, staged.identity
            )
        for backup in transaction.backups:
            self._unlink_if_identity(
                transaction, transaction.backup_fd, backup.name, backup.identity
            )
        _fsync(transaction.stage_fd)
        _fsync(transaction.backup_fd)

    @staticmethod
    def _unlink_if_identity(
        transaction: _Transaction, directory_fd: int, name: str, expected: _Identity
    ) -> None:
        if (directory_fd, name) in transaction.protected_entries:
            return
        try:
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISREG(status.st_mode) and _identity(status) == expected:
            os.unlink(name, dir_fd=directory_fd)

    def _close_transaction(self, transaction: _Transaction) -> None:
        if transaction.closed:
            return
        transaction.closed = True
        for descriptor in (transaction.stage_fd, transaction.backup_fd):
            with suppress(OSError):
                os.close(descriptor)
        with suppress(OSError):
            os.rmdir("stage", dir_fd=transaction.fd)
        with suppress(OSError):
            os.rmdir("backup", dir_fd=transaction.fd)
        with suppress(OSError):
            os.close(transaction.fd)
        with suppress(OSError):
            os.rmdir(transaction.name, dir_fd=transaction.parent_fd)
        with suppress(OSError):
            os.close(transaction.parent_fd)

    def _cleanup_created(self, anchor: _VaultAnchor, created: Iterable[_CreatedDirectory]) -> None:
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
