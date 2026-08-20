"""Guarded, recoverable publication inside one explicitly authorized vault."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from cove_book_forge.config import ObsidianOutputConfig
from cove_book_forge.contracts.books import MAX_BOOK_CHAPTERS
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs.managed import parse_obsidian_manifest, plan_obsidian_update
from cove_book_forge.outputs.obsidian_models import (
    MAX_OBSIDIAN_CARDS,
    ObsidianBookManifest,
    RenderedObsidianBook,
)
from cove_book_forge.path_safety import validate_relative_path

_O_DIRECTORY: Final = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK: Final = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY_FLAGS: Final = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
_READ_FLAGS: Final = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
_OWNER_CREATE_FLAGS: Final = os.O_RDWR | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
_OWNER_OPEN_FLAGS: Final = os.O_RDWR | _O_NOFOLLOW | _O_NONBLOCK
_MAX_MANIFEST_BYTES: Final = 2 * 1024 * 1024
_MAX_MARKDOWN_BYTES: Final = 32 * 1024 * 1024
_MAX_CANDIDATE_COUNT: Final = 10_000
_MAX_MANAGED_CHAPTERS: Final = MAX_BOOK_CHAPTERS
_MAX_MANAGED_CARDS: Final = MAX_OBSIDIAN_CARDS
_MAX_TOTAL_TRANSACTION_BYTES: Final = 256 * 1024 * 1024
_MAX_TRANSACTION_STATE_BYTES: Final = 16 * 1024 * 1024
_MAX_TRANSACTION_COUNT: Final = 64
_READ_CHUNK: Final = 1024 * 1024
_TRANSACTIONS_PATH: Final = ".cove-book-forge/.transactions"
_OWNER_NAME: Final = "owner.lock"
_TRANSACTION_NAME = re.compile(r"^tx-[0-9a-f]{32}$")
_RECOVERY_NAME = re.compile(r"^recovery-([0-9a-f]{32})\.json$")
_ROLLBACK_NAME = re.compile(r"^r[0-9]{6}-[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
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
_EFFECTIVE_ACCESS_DIR_FD_SUPPORTED: Final = os.access in os.supports_dir_fd
_EFFECTIVE_ACCESS_IDS_SUPPORTED: Final = os.access in os.supports_effective_ids

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
    except BaseException:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise


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


def check_obsidian_output_readiness(config: ObsidianOutputConfig) -> None:
    """Validate and close an enabled vault capability without creating output state."""
    with _VaultAnchor.capture(config) as anchor:
        anchor.verify()
        accessible = False
        probe_failed = not (_EFFECTIVE_ACCESS_DIR_FD_SUPPORTED and _EFFECTIVE_ACCESS_IDS_SUPPORTED)
        if not probe_failed:
            try:
                accessible = os.access(
                    ".",
                    os.W_OK | os.X_OK,
                    dir_fd=anchor.descriptor,
                    effective_ids=True,
                )
            except Exception:
                probe_failed = True
        anchor.verify()
        if probe_failed:
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        if not accessible:
            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED)


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
    NO_MOVE_CONFLICT = "no_move_conflict"
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


class _RollbackMoveStatus(StrEnum):
    INTENT = "intent"
    MOVED = "moved"
    NO_MOVE_CONFLICT = "no_move_conflict"
    AMBIGUOUS = "ambiguous"


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
    rollback_status: _RollbackMoveStatus | None = None


@dataclass
class _RecoveryEntry:
    original_path: str
    container: str
    moved_name: str
    record_name: str
    record_identity: _Identity
    durable: bool = False


class _TransactionPhase(StrEnum):
    PREPARED = "prepared"
    MANIFEST_PENDING = "manifest_pending"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class _DurableOldFile:
    path: str
    exists: bool
    parent_identity: _Identity
    identity: _Identity | None
    size: int | None
    digest: str | None


@dataclass(frozen=True)
class _DurableWrite:
    path: str
    stage_name: str
    parent_identity: _Identity
    identity: _Identity
    size: int
    digest: str


@dataclass(frozen=True)
class _TransactionJournal:
    transaction_name: str
    vault_identity: _Identity
    transaction_identity: _Identity
    owner_identity: _Identity
    stage_identity: _Identity
    backup_identity: _Identity
    manifest_path: str
    old_manifest_checksum: str | None
    new_manifest_checksum: str
    old_manifest_digest: str | None
    new_manifest_digest: str
    old_files: tuple[_DurableOldFile, ...]
    writes: tuple[_DurableWrite, ...]


@dataclass
class _Transaction:
    parent_fd: int
    name: str
    fd: int
    owner_fd: int
    stage_fd: int
    backup_fd: int
    identity: _Identity
    owner_identity: _Identity
    stage_identity: _Identity
    backup_identity: _Identity
    created_directories: list[_CreatedDirectory]
    staged: dict[str, _StagedFile] = field(default_factory=dict)
    backups: list[_BackupFile] = field(default_factory=list)
    published: list[_PublishedFile] = field(default_factory=list)
    protected_entries: set[tuple[int, str]] = field(default_factory=set)
    recoveries: dict[tuple[int, str], _RecoveryEntry] = field(default_factory=dict)
    expected_writes: Mapping[str, bytes] = field(default_factory=dict)
    journal: _TransactionJournal | None = None
    phase: _TransactionPhase | None = None
    state_records: dict[str, _Identity] = field(default_factory=dict)
    closed: bool = False


@dataclass(frozen=True)
class PublisherReceipt:
    rendered: RenderedObsidianBook
    changed_paths: tuple[str, ...]
    unchanged: bool


def _fsync_file(descriptor: int) -> None:
    os.fsync(descriptor)


def _fsync_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
            raise


def _bounded_directory_names(descriptor: int, limit: int) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            if len(names) >= limit:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            names.append(entry.name)
    return tuple(names)


def _try_owner_lock(descriptor: int) -> bool:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _unlock(descriptor: int) -> None:
    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _state_record_name(phase: _TransactionPhase) -> str:
    return f"state-{phase.value}.json"


def _journal_payload(journal: _TransactionJournal, phase: _TransactionPhase) -> dict[str, object]:
    payload: dict[str, object] = {
        "backup_identity": list(journal.backup_identity),
        "manifest": {
            "new_checksum": journal.new_manifest_checksum,
            "new_digest": journal.new_manifest_digest,
            "old_checksum": journal.old_manifest_checksum,
            "old_digest": journal.old_manifest_digest,
            "path": journal.manifest_path,
        },
        "old_files": [
            {
                "digest": item.digest,
                "exists": item.exists,
                "identity": None if item.identity is None else list(item.identity),
                "parent_identity": list(item.parent_identity),
                "path": item.path,
                "size": item.size,
            }
            for item in journal.old_files
        ],
        "phase": phase.value,
        "owner_identity": list(journal.owner_identity),
        "schema": 2,
        "stage_identity": list(journal.stage_identity),
        "transaction_identity": list(journal.transaction_identity),
        "transaction_name": journal.transaction_name,
        "vault_identity": list(journal.vault_identity),
        "writes": [
            {
                "digest": item.digest,
                "identity": list(item.identity),
                "parent_identity": list(item.parent_identity),
                "path": item.path,
                "size": item.size,
                "stage_name": item.stage_name,
            }
            for item in journal.writes
        ],
    }
    checksum_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["checksum"] = hashlib.sha256(checksum_bytes).hexdigest()
    return payload


def _journal_bytes(journal: _TransactionJournal, phase: _TransactionPhase) -> bytes:
    return json.dumps(
        _journal_payload(journal, phase),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_unique_json(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate transaction state key")
            result[key] = value
        return result

    value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("transaction state must be an object")
    return value


def _parse_identity(value: object) -> _Identity:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(part) is not int or part < 0 for part in value)
    ):
        raise ValueError("invalid identity")
    return value[0], value[1]


def _parse_journal_record(data: bytes) -> tuple[_TransactionJournal, _TransactionPhase]:
    payload = _load_unique_json(data)
    if set(payload) != {
        "backup_identity",
        "checksum",
        "manifest",
        "old_files",
        "owner_identity",
        "phase",
        "schema",
        "stage_identity",
        "transaction_identity",
        "transaction_name",
        "vault_identity",
        "writes",
    }:
        raise ValueError("invalid transaction state fields")
    checksum = payload.pop("checksum")
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not isinstance(checksum, str) or checksum != hashlib.sha256(canonical).hexdigest():
        raise ValueError("invalid transaction state checksum")
    if payload["schema"] != 2:
        raise ValueError("invalid transaction state schema")
    phase = _TransactionPhase(payload["phase"])
    transaction_name = payload["transaction_name"]
    if not isinstance(transaction_name, str) or not _TRANSACTION_NAME.fullmatch(transaction_name):
        raise ValueError("invalid transaction name")
    manifest = payload["manifest"]
    if not isinstance(manifest, dict) or set(manifest) != {
        "new_checksum",
        "new_digest",
        "old_checksum",
        "old_digest",
        "path",
    }:
        raise ValueError("invalid transaction manifest state")
    manifest_path = validate_relative_path(manifest["path"])
    new_checksum = manifest["new_checksum"]
    new_digest = manifest["new_digest"]
    old_checksum = manifest["old_checksum"]
    old_digest = manifest["old_digest"]
    if (
        not isinstance(new_checksum, str)
        or not _HEX_64.fullmatch(new_checksum)
        or not isinstance(new_digest, str)
        or not _HEX_64.fullmatch(new_digest)
        or (old_checksum is not None and not isinstance(old_checksum, str))
        or (isinstance(old_checksum, str) and not _HEX_64.fullmatch(old_checksum))
        or (old_digest is not None and not isinstance(old_digest, str))
        or (isinstance(old_digest, str) and not _HEX_64.fullmatch(old_digest))
    ):
        raise ValueError("invalid transaction manifest digest")
    raw_old_files = payload["old_files"]
    raw_writes = payload["writes"]
    if (
        not isinstance(raw_old_files, list)
        or not isinstance(raw_writes, list)
        or len(raw_old_files) > _MAX_CANDIDATE_COUNT
        or len(raw_writes) > _MAX_CANDIDATE_COUNT
    ):
        raise ValueError("invalid transaction file count")
    old_files: list[_DurableOldFile] = []
    old_paths: set[str] = set()
    total_old_size = 0
    for raw in raw_old_files:
        if not isinstance(raw, dict) or set(raw) != {
            "digest",
            "exists",
            "identity",
            "parent_identity",
            "path",
            "size",
        }:
            raise ValueError("invalid old file state")
        path = validate_relative_path(raw["path"])
        exists = raw["exists"]
        size = raw["size"]
        digest = raw["digest"]
        identity_value = raw["identity"]
        if path in old_paths or type(exists) is not bool:
            raise ValueError("duplicate old file state")
        old_paths.add(path)
        if exists:
            if (
                type(size) is not int
                or size < 0
                or not isinstance(digest, str)
                or not _HEX_64.fullmatch(digest)
            ):
                raise ValueError("invalid old file digest")
            identity = _parse_identity(identity_value)
            per_file_limit = _MAX_MANIFEST_BYTES if path.endswith(".json") else _MAX_MARKDOWN_BYTES
            if size > per_file_limit:
                raise ValueError("old file exceeds transaction limit")
            total_old_size += size
        else:
            if size is not None or digest is not None or identity_value is not None:
                raise ValueError("invalid absent old file")
            identity = None
        old_files.append(
            _DurableOldFile(
                path,
                exists,
                _parse_identity(raw["parent_identity"]),
                identity,
                size,
                digest,
            )
        )
    writes: list[_DurableWrite] = []
    write_paths: set[str] = set()
    stage_names: set[str] = set()
    total_write_size = 0
    for raw in raw_writes:
        if not isinstance(raw, dict) or set(raw) != {
            "digest",
            "identity",
            "parent_identity",
            "path",
            "size",
            "stage_name",
        }:
            raise ValueError("invalid write state")
        path = validate_relative_path(raw["path"])
        stage_name = raw["stage_name"]
        size = raw["size"]
        digest = raw["digest"]
        if (
            path in write_paths
            or not isinstance(stage_name, str)
            or not re.fullmatch(r"s[0-9]{6}", stage_name)
            or stage_name in stage_names
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or not _HEX_64.fullmatch(digest)
        ):
            raise ValueError("invalid write state values")
        write_paths.add(path)
        stage_names.add(stage_name)
        per_file_limit = _MAX_MANIFEST_BYTES if path.endswith(".json") else _MAX_MARKDOWN_BYTES
        if size > per_file_limit:
            raise ValueError("write exceeds transaction limit")
        total_write_size += size
        writes.append(
            _DurableWrite(
                path,
                stage_name,
                _parse_identity(raw["parent_identity"]),
                _parse_identity(raw["identity"]),
                size,
                digest,
            )
        )
    if manifest_path not in old_paths or manifest_path not in write_paths:
        raise ValueError("manifest is outside transaction file state")
    old_manifest = next(item for item in old_files if item.path == manifest_path)
    if (
        total_old_size + total_write_size > _MAX_TOTAL_TRANSACTION_BYTES
        or old_manifest.digest != old_digest
        or old_manifest.exists != (old_checksum is not None)
        or (old_checksum is None) != (old_digest is None)
    ):
        raise ValueError("invalid transaction aggregate state")
    journal = _TransactionJournal(
        transaction_name=transaction_name,
        vault_identity=_parse_identity(payload["vault_identity"]),
        transaction_identity=_parse_identity(payload["transaction_identity"]),
        owner_identity=_parse_identity(payload["owner_identity"]),
        stage_identity=_parse_identity(payload["stage_identity"]),
        backup_identity=_parse_identity(payload["backup_identity"]),
        manifest_path=manifest_path,
        old_manifest_checksum=old_checksum,
        new_manifest_checksum=new_checksum,
        old_manifest_digest=old_digest,
        new_manifest_digest=new_digest,
        old_files=tuple(old_files),
        writes=tuple(writes),
    )
    return journal, phase


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
            self._recover_transactions(anchor)
            initial = render(None)
            self._validate_rendered_budget(initial)
            initial_book_key = initial.manifest.book_key
            initial_paths = tuple(initial.files)
            del initial
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
            committed_bundle_verified = False
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
                self._prepare_durable_transaction(
                    anchor,
                    transaction,
                    snapshots,
                    (*plan.writes, *plan.removals),
                    manifest_path,
                    previous,
                    rendered,
                )
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
                assert transaction.journal is not None
                self._durabilize_new_bundle(anchor, transaction.journal)
                self._persist_transaction_phase(transaction, _TransactionPhase.COMMITTED)
                committed = True
                if not self._new_bundle_is_complete(anchor, transaction.journal, {}):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                committed_bundle_verified = True
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
                    if not committed_bundle_verified:
                        self._close_recovery_descriptors(transaction)
                        if not isinstance(exc, Exception):
                            raise exc from None
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
                    cleanup_signal = self._finish_committed_cleanup(
                        anchor,
                        transaction,
                        created,
                        (
                            exc
                            if not isinstance(exc, Exception)
                            or isinstance(exc, OSError)
                            and exc.errno == errno.EBADF
                            else None
                        ),
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
                    except BaseException as rollback_exc:
                        rollback_safe = self._terminalize_rolled_back(anchor, transaction)
                        if isinstance(exc, Exception):
                            pending = rollback_exc
                    else:
                        rollback_safe = rollback_safe and self._terminalize_rolled_back(
                            anchor, transaction
                        )
                    if transaction.protected_entries or transaction.recoveries:
                        rollback_safe = False
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

    def _terminalize_rolled_back(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
    ) -> bool:
        journal = transaction.journal
        if transaction.phase is None:
            return True
        if journal is None or not self._old_bundle_is_complete(anchor, journal):
            return False
        self._durabilize_old_bundle(anchor, journal)
        self._persist_transaction_phase(transaction, _TransactionPhase.ROLLED_BACK)
        if not self._old_bundle_is_complete(anchor, journal):
            self._close_recovery_descriptors(transaction)
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        self._cleanup_rolled_back(transaction)
        return not transaction.protected_entries and not transaction.recoveries

    def _finish_committed_cleanup(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        created: list[_CreatedDirectory],
        pending_signal: BaseException | None,
    ) -> BaseException | None:
        cleanup_complete = False
        last_cleanup_error: BaseException | None = None
        for _attempt in range(2):
            try:
                self._cleanup_committed(transaction)
            except BaseException as cleanup_exc:
                last_cleanup_error = cleanup_exc
                if pending_signal is None and (
                    not isinstance(cleanup_exc, Exception)
                    or isinstance(cleanup_exc, OSError)
                    and cleanup_exc.errno == errno.EBADF
                ):
                    pending_signal = cleanup_exc
                continue
            cleanup_complete = True
            break
        if not cleanup_complete:
            self._close_recovery_descriptors(transaction)
            return (
                pending_signal or last_cleanup_error or _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            )
        close_complete = False
        last_close_error: BaseException | None = None
        for _attempt in range(2):
            try:
                self._close_transaction(transaction)
            except BaseException as cleanup_exc:
                last_close_error = cleanup_exc
                if pending_signal is None and not isinstance(cleanup_exc, Exception):
                    pending_signal = cleanup_exc
                continue
            close_complete = True
            break
        if not close_complete:
            self._close_recovery_descriptors(transaction)
            return (
                pending_signal or last_close_error or _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            )
        created_cleanup_complete = False
        last_created_error: BaseException | None = None
        for _attempt in range(2):
            try:
                self._cleanup_created(anchor, created)
            except BaseException as cleanup_exc:
                last_created_error = cleanup_exc
                if pending_signal is None and not isinstance(cleanup_exc, Exception):
                    pending_signal = cleanup_exc
                continue
            created_cleanup_complete = True
            break
        if not created_cleanup_complete:
            return (
                pending_signal or last_created_error or _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            )
        return pending_signal

    def _recover_transactions(self, anchor: _VaultAnchor) -> None:
        anchor.verify()
        parent = self._open_directory(anchor, _TRANSACTIONS_PATH, create=False)
        if parent is None:
            return
        parent_locked = False
        try:
            if not _try_owner_lock(parent):
                return
            parent_locked = True
            names = _bounded_directory_names(parent, _MAX_TRANSACTION_COUNT)
            for name in sorted(names):
                if not _TRANSACTION_NAME.fullmatch(name):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                transaction = self._open_recovery_transaction(anchor, parent, name)
                if transaction is None:
                    continue
                try:
                    self._recover_transaction(anchor, transaction)
                except BaseException:
                    self._close_recovery_descriptors(transaction)
                    raise
                try:
                    os.stat(name, dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            anchor.verify()
        except ForgeException:
            raise
        except PermissionError:
            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
        except (OSError, RuntimeError, ValueError, UnicodeError, json.JSONDecodeError):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        finally:
            if parent_locked:
                with suppress(OSError):
                    _unlock(parent)
            with suppress(OSError):
                os.close(parent)

    def _open_recovery_transaction(
        self, anchor: _VaultAnchor, parent_fd: int, name: str
    ) -> _Transaction | None:
        tx_fd: int | None = None
        owner_fd: int | None = None
        stage_fd: int | None = None
        backup_fd: int | None = None
        owned_parent: int | None = None
        try:
            entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            tx_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            tx_identity = _identity(os.fstat(tx_fd))
            if tx_identity != _identity(entry):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            root_names = set(
                _bounded_directory_names(tx_fd, _MAX_CANDIDATE_COUNT + len(_TransactionPhase) + 4)
            )
            owner_identity: _Identity | None = None
            if _OWNER_NAME in root_names:
                owner_entry = os.stat(_OWNER_NAME, dir_fd=tx_fd, follow_symlinks=False)
                if not stat.S_ISREG(owner_entry.st_mode) or owner_entry.st_size != 0:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                owner_fd = os.open(_OWNER_NAME, _OWNER_OPEN_FLAGS, dir_fd=tx_fd)
                owner_status = os.fstat(owner_fd)
                owner_identity = _identity(owner_status)
                if (
                    not stat.S_ISREG(owner_status.st_mode)
                    or owner_status.st_size != 0
                    or owner_identity != _identity(owner_entry)
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                if not _try_owner_lock(owner_fd):
                    return None
            state_names = {_state_record_name(phase) for phase in _TransactionPhase}
            if not root_names & state_names:
                self._reclaim_prejournal_transaction(
                    parent_fd,
                    name,
                    tx_fd,
                    tx_identity,
                    owner_identity,
                    root_names,
                )
                return None
            if owner_fd is None or owner_identity is None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            stage_fd = os.open("stage", _DIRECTORY_FLAGS, dir_fd=tx_fd)
            backup_fd = os.open("backup", _DIRECTORY_FLAGS, dir_fd=tx_fd)
            stage_identity = _identity(os.fstat(stage_fd))
            backup_identity = _identity(os.fstat(backup_fd))
            state_records: dict[str, _Identity] = {}
            found_phases: set[_TransactionPhase] = set()
            selected_journal: _TransactionJournal | None = None
            selected_phase: _TransactionPhase | None = None
            for phase in _TransactionPhase:
                record_name = _state_record_name(phase)
                try:
                    snapshot = self._snapshot_in_directory(tx_fd, record_name)
                except FileNotFoundError:
                    continue
                if snapshot.size is None or snapshot.size > _MAX_TRANSACTION_STATE_BYTES:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                assert snapshot.data is not None and snapshot.identity is not None
                journal, parsed_phase = _parse_journal_record(snapshot.data)
                if parsed_phase is not phase:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                if selected_journal is not None and journal != selected_journal:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                selected_journal = journal
                selected_phase = phase
                found_phases.add(phase)
                state_records[record_name] = snapshot.identity
            if selected_journal is None or selected_phase is None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if not self._valid_recovery_phase_set(found_phases, selected_phase):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if (
                selected_journal.transaction_name != name
                or selected_journal.vault_identity != anchor.identity
                or selected_journal.transaction_identity != tx_identity
                or selected_journal.owner_identity != owner_identity
                or selected_journal.stage_identity != stage_identity
                or selected_journal.backup_identity != backup_identity
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            owned_parent = os.dup(parent_fd)
            transaction = _Transaction(
                parent_fd=owned_parent,
                name=name,
                fd=tx_fd,
                owner_fd=owner_fd,
                stage_fd=stage_fd,
                backup_fd=backup_fd,
                identity=tx_identity,
                owner_identity=owner_identity,
                stage_identity=stage_identity,
                backup_identity=backup_identity,
                created_directories=[],
                journal=selected_journal,
                phase=selected_phase,
                state_records=state_records,
            )
            owned_parent = None
            tx_fd = None
            owner_fd = None
            stage_fd = None
            backup_fd = None
            try:
                self._load_recovery_entries(transaction)
                self._validate_recovery_directory_entries(transaction)
            except BaseException:
                self._close_recovery_descriptors(transaction)
                raise
            return transaction
        finally:
            for descriptor in (backup_fd, stage_fd, owner_fd, tx_fd, owned_parent):
                if descriptor is not None:
                    with suppress(OSError):
                        os.close(descriptor)

    @staticmethod
    def _valid_recovery_phase_set(
        found: set[_TransactionPhase], selected: _TransactionPhase
    ) -> bool:
        if selected is _TransactionPhase.PREPARED:
            return found == {_TransactionPhase.PREPARED}
        if selected is _TransactionPhase.MANIFEST_PENDING:
            return found == {
                _TransactionPhase.PREPARED,
                _TransactionPhase.MANIFEST_PENDING,
            }
        chains = (
            (
                _TransactionPhase.PREPARED,
                _TransactionPhase.MANIFEST_PENDING,
                _TransactionPhase.COMMITTED,
            ),
            (_TransactionPhase.PREPARED, _TransactionPhase.ROLLED_BACK),
            (
                _TransactionPhase.PREPARED,
                _TransactionPhase.MANIFEST_PENDING,
                _TransactionPhase.ROLLED_BACK,
            ),
        )
        return any(
            chain[-1] is selected and found == set(chain[index:])
            for chain in chains
            for index in range(len(chain))
        )

    def _reclaim_prejournal_transaction(
        self,
        parent_fd: int,
        name: str,
        transaction_fd: int,
        transaction_identity: _Identity,
        owner_identity: _Identity | None,
        root_names: set[str],
    ) -> None:
        if not root_names <= {_OWNER_NAME, "stage", "backup"}:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        for directory_name in ("stage", "backup"):
            if directory_name not in root_names:
                continue
            entry = os.stat(directory_name, dir_fd=transaction_fd, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            descriptor = os.open(directory_name, _DIRECTORY_FLAGS, dir_fd=transaction_fd)
            try:
                if _identity(os.fstat(descriptor)) != _identity(entry) or _bounded_directory_names(
                    descriptor, 1
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            finally:
                os.close(descriptor)
            current = os.stat(directory_name, dir_fd=transaction_fd, follow_symlinks=False)
            if _identity(current) != _identity(entry):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            os.rmdir(directory_name, dir_fd=transaction_fd)
        if owner_identity is not None:
            current = os.stat(_OWNER_NAME, dir_fd=transaction_fd, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or _identity(current) != owner_identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            os.unlink(_OWNER_NAME, dir_fd=transaction_fd)
        _fsync_directory(transaction_fd)
        current_transaction = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(current_transaction.st_mode)
            or _identity(current_transaction) != transaction_identity
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        os.rmdir(name, dir_fd=parent_fd)
        _fsync_directory(parent_fd)

    def _load_recovery_entries(self, transaction: _Transaction) -> None:
        journal = transaction.journal
        assert journal is not None
        old_by_path = {item.path: item for item in journal.old_files}
        writes_by_path = {item.path: item for item in journal.writes}
        allowed_root = {_OWNER_NAME, "stage", "backup", *transaction.state_records}
        root_names = _bounded_directory_names(
            transaction.fd, _MAX_CANDIDATE_COUNT + len(transaction.state_records) + 3
        )
        for name in root_names:
            if name in allowed_root:
                continue
            if _ROLLBACK_NAME.fullmatch(name):
                continue
            match = _RECOVERY_NAME.fullmatch(name)
            if match is None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            snapshot = self._snapshot_in_directory(transaction.fd, name)
            if snapshot.data is None or snapshot.identity is None or snapshot.size is None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if snapshot.size > _MAX_TRANSACTION_STATE_BYTES:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            payload = _load_unique_json(snapshot.data)
            if set(payload) != {
                "container",
                "moved_name",
                "original_path",
                "recovery_id",
                "schema",
            }:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            recovery_id = payload["recovery_id"]
            container = payload["container"]
            moved_name = payload["moved_name"]
            original_path = validate_relative_path(payload["original_path"])
            if (
                payload["schema"] != 1
                or recovery_id != match.group(1)
                or container not in {"backup", "transaction"}
                or not isinstance(moved_name, str)
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if container == "backup":
                valid_mapping = (
                    re.fullmatch(r"b[0-9]{6}", moved_name) is not None
                    and original_path in old_by_path
                    and old_by_path[original_path].exists
                )
                directory_fd = transaction.backup_fd
            else:
                valid_mapping = (
                    _ROLLBACK_NAME.fullmatch(moved_name) is not None
                    and original_path in writes_by_path
                )
                directory_fd = transaction.fd
            if not valid_mapping:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            key = (directory_fd, moved_name)
            if key in transaction.recoveries:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            recovery = _RecoveryEntry(
                original_path=original_path,
                container=container,
                moved_name=moved_name,
                record_name=name,
                record_identity=snapshot.identity,
                durable=True,
            )
            transaction.recoveries[key] = recovery
            transaction.protected_entries.add(key)
        allowed_rollback_entries = {
            moved_name
            for directory_fd, moved_name in transaction.recoveries
            if directory_fd == transaction.fd
        }
        if {
            name for name in root_names if _ROLLBACK_NAME.fullmatch(name)
        } - allowed_rollback_entries:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        for (directory_fd, moved_name), recovery in tuple(transaction.recoveries.items()):
            try:
                stored = self._snapshot_in_directory(directory_fd, moved_name)
            except FileNotFoundError:
                continue
            if directory_fd == transaction.fd:
                write = writes_by_path[recovery.original_path]
                if not self._snapshot_matches_write(stored, write, allow_missing_parent=True):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                continue
            old = old_by_path[recovery.original_path]
            if not self._snapshot_matches_old(stored, old, allow_missing_parent=True):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            assert old.identity is not None and old.size is not None and old.digest is not None
            parent_path, _, source_name = old.path.rpartition("/")
            transaction.backups.append(
                _BackupFile(
                    path=old.path,
                    parent_path=parent_path,
                    source_name=source_name,
                    name=moved_name,
                    parent_identity=old.parent_identity,
                    identity=old.identity,
                    digest=old.digest,
                    size=old.size,
                    status=_BackupStatus.VERIFIED,
                )
            )

    def _validate_recovery_directory_entries(self, transaction: _Transaction) -> None:
        journal = transaction.journal
        assert journal is not None
        allowed_stage = {item.stage_name for item in journal.writes}
        stage_entries = set(_bounded_directory_names(transaction.stage_fd, _MAX_CANDIDATE_COUNT))
        if not stage_entries <= allowed_stage:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        allowed_backup = {
            moved_name
            for directory_fd, moved_name in transaction.recoveries
            if directory_fd == transaction.backup_fd
        }
        if (
            not set(_bounded_directory_names(transaction.backup_fd, _MAX_CANDIDATE_COUNT))
            <= allowed_backup
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _recover_transaction(self, anchor: _VaultAnchor, transaction: _Transaction) -> None:
        journal = transaction.journal
        phase = transaction.phase
        assert journal is not None and phase is not None
        stage_bytes = self._recovery_stage_bytes(
            transaction,
            require_all=phase in {_TransactionPhase.PREPARED, _TransactionPhase.MANIFEST_PENDING},
        )
        commit_visible = self._new_bundle_is_complete(anchor, journal, stage_bytes)
        if phase is _TransactionPhase.COMMITTED:
            if not commit_visible:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._recover_committed_transaction(transaction)
        elif phase is _TransactionPhase.ROLLED_BACK:
            if not self._old_bundle_is_complete(anchor, journal):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._recover_rolled_back_transaction(transaction)
        elif phase is _TransactionPhase.MANIFEST_PENDING and commit_visible:
            self._durabilize_new_bundle(anchor, journal)
            self._persist_transaction_phase(transaction, _TransactionPhase.COMMITTED)
            if not self._new_bundle_is_complete(anchor, journal, {}):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._recover_committed_transaction(transaction)
        else:
            self._recover_precommit_transaction(anchor, transaction, stage_bytes)
        self._close_transaction(transaction)

    def _recovery_stage_bytes(
        self, transaction: _Transaction, *, require_all: bool
    ) -> dict[str, bytes]:
        journal = transaction.journal
        assert journal is not None
        result: dict[str, bytes] = {}
        transaction.expected_writes = {}
        for item in journal.writes:
            transaction.staged[item.path] = _StagedFile(
                item.path, item.stage_name, item.identity, item.digest
            )
            try:
                snapshot = self._snapshot_in_directory(transaction.stage_fd, item.stage_name)
            except FileNotFoundError:
                if require_all:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
                continue
            if not self._snapshot_matches_write(snapshot, item, allow_missing_parent=True):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            assert snapshot.data is not None
            result[item.path] = snapshot.data
        transaction.expected_writes = result
        return result

    def _new_bundle_is_complete(
        self,
        anchor: _VaultAnchor,
        journal: _TransactionJournal,
        stage_bytes: Mapping[str, bytes],
    ) -> bool:
        writes = {item.path: item for item in journal.writes}
        for path, item in writes.items():
            try:
                current = self._read_snapshot(anchor, path)
            except ForgeException:
                return False
            if not self._snapshot_matches_write(current, item):
                return False
            expected = stage_bytes.get(path)
            if expected is not None and current.data != expected:
                return False
        for old in journal.old_files:
            if old.path in writes:
                continue
            try:
                current = self._read_snapshot(anchor, old.path)
            except ForgeException:
                return False
            if current.exists:
                return False
        manifest = writes[journal.manifest_path]
        if manifest.digest != journal.new_manifest_digest:
            return False
        try:
            manifest_snapshot = self._read_snapshot(anchor, journal.manifest_path)
            assert manifest_snapshot.data is not None
            parsed = parse_obsidian_manifest(manifest_snapshot.data)
        except (AssertionError, ForgeException):
            return False
        return parsed.checksum == journal.new_manifest_checksum

    def _old_bundle_is_complete(self, anchor: _VaultAnchor, journal: _TransactionJournal) -> bool:
        old_by_path = {item.path: item for item in journal.old_files}
        for old in journal.old_files:
            try:
                current = self._read_snapshot(anchor, old.path)
            except ForgeException:
                return False
            if not self._snapshot_matches_old(current, old):
                return False
        manifest = old_by_path[journal.manifest_path]
        if not manifest.exists:
            return journal.old_manifest_checksum is None
        try:
            snapshot = self._read_snapshot(anchor, journal.manifest_path)
            assert snapshot.data is not None
            parsed = parse_obsidian_manifest(snapshot.data)
        except (AssertionError, ForgeException):
            return False
        return parsed.checksum == journal.old_manifest_checksum

    def _durabilize_new_bundle(self, anchor: _VaultAnchor, journal: _TransactionJournal) -> None:
        anchor.verify()
        manifest_bytes: bytes | None = None
        for write in journal.writes:
            data = self._durabilize_regular_file(
                anchor,
                write.path,
                write.parent_identity,
                write.identity,
                write.size,
                write.digest,
            )
            if write.path == journal.manifest_path:
                manifest_bytes = data
        write_paths = {write.path for write in journal.writes}
        for old in journal.old_files:
            if old.path not in write_paths:
                self._durabilize_absence(anchor, old.path, old.parent_identity)
        if manifest_bytes is None:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        try:
            parsed = parse_obsidian_manifest(manifest_bytes)
        except ForgeException:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        if parsed.checksum != journal.new_manifest_checksum:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        anchor.verify()

    def _durabilize_old_bundle(self, anchor: _VaultAnchor, journal: _TransactionJournal) -> None:
        anchor.verify()
        manifest_bytes: bytes | None = None
        for old in journal.old_files:
            if old.exists:
                if old.identity is None or old.size is None or old.digest is None:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                data = self._durabilize_regular_file(
                    anchor,
                    old.path,
                    old.parent_identity,
                    old.identity,
                    old.size,
                    old.digest,
                )
                if old.path == journal.manifest_path:
                    manifest_bytes = data
            else:
                self._durabilize_absence(anchor, old.path, old.parent_identity)
        if journal.old_manifest_checksum is None:
            if manifest_bytes is not None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        else:
            if manifest_bytes is None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            try:
                parsed = parse_obsidian_manifest(manifest_bytes)
            except ForgeException:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
            if parsed.checksum != journal.old_manifest_checksum:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        anchor.verify()

    def _durabilize_regular_file(
        self,
        anchor: _VaultAnchor,
        path: str,
        parent_identity: _Identity,
        identity: _Identity,
        size: int,
        digest: str,
    ) -> bytes:
        parent_path, _, name = path.rpartition("/")
        parent = self._open_directory(anchor, parent_path, create=False)
        if parent is None:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        descriptor: int | None = None
        try:
            if _identity(os.fstat(parent)) != parent_identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
            descriptor = os.open(name, _READ_FLAGS, dir_fd=parent)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(entry.st_mode)
                or not stat.S_ISREG(before.st_mode)
                or _identity(entry) != identity
                or _identity(before) != identity
                or entry.st_size != size
                or before.st_size != size
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            data = _read_descriptor(descriptor, size)
            after = os.fstat(descriptor)
            if (
                _identity(after) != identity
                or after.st_size != size
                or hashlib.sha256(data).hexdigest() != digest
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            _fsync_file(descriptor)
            synced = os.fstat(descriptor)
            if _identity(synced) != identity or synced.st_size != size:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            _fsync_directory(parent)
            if _identity(os.fstat(parent)) != parent_identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            return data
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)
            with suppress(OSError):
                os.close(parent)

    def _durabilize_absence(
        self,
        anchor: _VaultAnchor,
        path: str,
        parent_identity: _Identity,
    ) -> None:
        parent_path, _, name = path.rpartition("/")
        parent = self._open_directory(anchor, parent_path, create=False)
        if parent is None:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        try:
            if _identity(os.fstat(parent)) != parent_identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            _fsync_directory(parent)
            if _identity(os.fstat(parent)) != parent_identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        finally:
            with suppress(OSError):
                os.close(parent)

    def _recover_precommit_transaction(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        stage_bytes: Mapping[str, bytes],
    ) -> None:
        journal = transaction.journal
        assert journal is not None
        self._reconcile_recovery_rollback_intents(anchor, transaction, stage_bytes)
        old_by_path = {item.path: item for item in journal.old_files}
        recoverable_paths = {item.path for item in transaction.backups}
        for write in reversed(journal.writes):
            current = self._read_snapshot(anchor, write.path)
            old = old_by_path[write.path]
            if self._snapshot_matches_write(current, write):
                expected = stage_bytes.get(write.path)
                if expected is None or current.data != expected:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                published = _PublishedFile(
                    path=write.path,
                    parent_path=write.path.rpartition("/")[0],
                    name=write.path.rpartition("/")[2],
                    parent_identity=write.parent_identity,
                    identity=write.identity,
                    digest=write.digest,
                    size=write.size,
                    data=expected,
                    status=_PublishedStatus.PUBLISHED,
                )
                transaction.published.append(published)
                removed = False
                for _attempt in range(3):
                    if self._remove_published(anchor, transaction, published):
                        removed = True
                        break
                if not removed:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            elif not self._snapshot_matches_old(current, old) and not (
                old.exists and not current.exists and old.path in recoverable_paths
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        backups_by_path = {item.path: item for item in transaction.backups}
        for old in journal.old_files:
            current = self._read_snapshot(anchor, old.path)
            backup = backups_by_path.get(old.path)
            if old.exists:
                if self._snapshot_matches_old(current, old):
                    if backup is not None:
                        backup.status = _BackupStatus.RESTORED
                    else:
                        self._forget_absent_backup_record(transaction, old.path)
                    continue
                if (
                    current.exists
                    or backup is None
                    or not self._restore_backup(anchor, transaction, backup)
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            elif current.exists:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        for old in journal.old_files:
            if not self._snapshot_matches_old(self._read_snapshot(anchor, old.path), old):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        old_manifest = old_by_path[journal.manifest_path]
        manifest_snapshot = self._read_snapshot(anchor, journal.manifest_path)
        if old_manifest.exists:
            try:
                assert manifest_snapshot.data is not None
                parsed = parse_obsidian_manifest(manifest_snapshot.data)
            except (AssertionError, ForgeException):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
            if parsed.checksum != journal.old_manifest_checksum:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        elif journal.old_manifest_checksum is not None or manifest_snapshot.exists:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        self._durabilize_old_bundle(anchor, journal)
        self._persist_transaction_phase(transaction, _TransactionPhase.ROLLED_BACK)
        if not self._old_bundle_is_complete(anchor, journal):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        self._cleanup_rolled_back(transaction)
        if transaction.protected_entries or transaction.recoveries:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _reconcile_recovery_rollback_intents(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        stage_bytes: Mapping[str, bytes],
    ) -> None:
        journal = transaction.journal
        assert journal is not None
        writes = {item.path: item for item in journal.writes}
        for (directory_fd, moved_name), recovery in tuple(transaction.recoveries.items()):
            if directory_fd != transaction.fd:
                continue
            write = writes[recovery.original_path]
            expected = stage_bytes[write.path]
            current = self._read_snapshot(anchor, write.path)
            if current.parent_identity != write.parent_identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            current_is_write = self._snapshot_matches_write(current, write)
            if current_is_write and current.data != expected:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            try:
                moved = self._snapshot_in_directory(transaction.fd, moved_name)
            except FileNotFoundError:
                moved = _FileSnapshot(False)
            moved_is_write = self._snapshot_matches_write(moved, write, allow_missing_parent=True)
            if moved.exists and (not moved_is_write or moved.data != expected):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if current_is_write:
                if moved.exists:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                if not self._forget_moved(transaction, transaction.fd, moved_name):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                continue
            if current.exists:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if not moved.exists:
                if not self._forget_moved(transaction, transaction.fd, moved_name):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                continue
            published = _PublishedFile(
                path=write.path,
                parent_path=write.path.rpartition("/")[0],
                name=write.path.rpartition("/")[2],
                parent_identity=write.parent_identity,
                identity=write.identity,
                digest=write.digest,
                size=write.size,
                data=expected,
                status=_PublishedStatus.PUBLISHED,
                rollback_name=moved_name,
                rollback_status=_RollbackMoveStatus.MOVED,
            )
            parent = self._open_directory(anchor, published.parent_path, create=False)
            if parent is None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            try:
                if _identity(
                    os.fstat(parent)
                ) != write.parent_identity or not self._settle_published_move(
                    transaction, published, parent
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            finally:
                os.close(parent)

    def _forget_absent_backup_record(self, transaction: _Transaction, path: str) -> None:
        for (directory_fd, moved_name), recovery in tuple(transaction.recoveries.items()):
            if recovery.original_path != path:
                continue
            if self._entry_exists(directory_fd, moved_name):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if not self._forget_moved(transaction, directory_fd, moved_name):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _recover_committed_transaction(self, transaction: _Transaction) -> None:
        for (directory_fd, moved_name), _recovery in tuple(transaction.recoveries.items()):
            if not self._entry_exists(directory_fd, moved_name) and not self._forget_moved(
                transaction, directory_fd, moved_name
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        self._cleanup_committed(transaction)
        if transaction.protected_entries or transaction.recoveries:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _recover_rolled_back_transaction(self, transaction: _Transaction) -> None:
        for backup in transaction.backups:
            backup.status = _BackupStatus.RESTORED
        self._cleanup_rolled_back(transaction)
        if transaction.protected_entries or transaction.recoveries:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    @staticmethod
    def _snapshot_matches_old(
        snapshot: _FileSnapshot,
        old: _DurableOldFile,
        *,
        allow_missing_parent: bool = False,
    ) -> bool:
        if not old.exists:
            return not snapshot.exists
        return (
            snapshot.exists
            and (allow_missing_parent or snapshot.parent_identity == old.parent_identity)
            and snapshot.identity == old.identity
            and snapshot.size == old.size
            and snapshot.digest == old.digest
            and snapshot.data is not None
        )

    @staticmethod
    def _snapshot_matches_write(
        snapshot: _FileSnapshot,
        write: _DurableWrite,
        *,
        allow_missing_parent: bool = False,
    ) -> bool:
        return (
            snapshot.exists
            and (allow_missing_parent or snapshot.parent_identity == write.parent_identity)
            and snapshot.identity == write.identity
            and snapshot.size == write.size
            and snapshot.digest == write.digest
            and snapshot.data is not None
        )

    @staticmethod
    def _close_recovery_descriptors(transaction: _Transaction) -> None:
        if transaction.closed:
            return
        transaction.closed = True
        for descriptor in (
            transaction.stage_fd,
            transaction.backup_fd,
            transaction.owner_fd,
            transaction.fd,
            transaction.parent_fd,
        ):
            with suppress(OSError):
                os.close(descriptor)

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
        owner_fd: int | None = None
        tx_identity: _Identity | None = None
        owner_identity: _Identity | None = None
        parent_locked = False
        try:
            fcntl.flock(parent_fd, fcntl.LOCK_EX)
            parent_locked = True
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            created_status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not stat.S_ISDIR(created_status.st_mode):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            tx_identity = _identity(created_status)
            tx_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            if _identity(os.fstat(tx_fd)) != tx_identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            owner_fd = os.open(_OWNER_NAME, _OWNER_CREATE_FLAGS, 0o600, dir_fd=tx_fd)
            owner_status = os.fstat(owner_fd)
            if not stat.S_ISREG(owner_status.st_mode) or owner_status.st_size != 0:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            owner_identity = _identity(owner_status)
            if not _try_owner_lock(owner_fd):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            _fsync_file(owner_fd)
            _fsync_directory(tx_fd)
            os.mkdir("stage", mode=0o700, dir_fd=tx_fd)
            os.mkdir("backup", mode=0o700, dir_fd=tx_fd)
            stage_fd = os.open("stage", _DIRECTORY_FLAGS, dir_fd=tx_fd)
            backup_fd = os.open("backup", _DIRECTORY_FLAGS, dir_fd=tx_fd)
            stage_identity = _identity(os.fstat(stage_fd))
            backup_identity = _identity(os.fstat(backup_fd))
            _fsync_directory(parent_fd)
            assert tx_identity is not None and owner_identity is not None
            transaction = _Transaction(
                parent_fd,
                name,
                tx_fd,
                owner_fd,
                stage_fd,
                backup_fd,
                tx_identity,
                owner_identity,
                stage_identity,
                backup_identity,
                created,
            )
            _unlock(parent_fd)
            parent_locked = False
            return transaction
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
                if owner_identity is not None:
                    with suppress(OSError):
                        self._unlink_named_identity(tx_fd, _OWNER_NAME, owner_identity)
                if owner_fd is not None:
                    with suppress(OSError):
                        os.close(owner_fd)
                    owner_fd = None
                with suppress(OSError):
                    os.close(tx_fd)
            if tx_identity is not None:
                try:
                    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    if stat.S_ISDIR(current.st_mode) and _identity(current) == tx_identity:
                        os.rmdir(name, dir_fd=parent_fd)
                        _fsync_directory(parent_fd)
                except (FileNotFoundError, OSError):
                    pass
            with suppress(OSError):
                os.close(parent_fd)
            if isinstance(exc, OSError):
                raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
            raise
        finally:
            if parent_locked:
                with suppress(OSError):
                    _unlock(parent_fd)

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
                _fsync_file(descriptor)
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
        _fsync_directory(transaction.stage_fd)

    def _prepare_durable_transaction(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        snapshots: Mapping[str, _FileSnapshot],
        mutation_paths: Iterable[str],
        manifest_path: str,
        previous: ObsidianBookManifest | None,
        rendered: RenderedObsidianBook,
    ) -> None:
        old_files: list[_DurableOldFile] = []
        for path in sorted(set(mutation_paths)):
            snapshot = snapshots[path]
            if snapshot.parent_identity is None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            old_files.append(
                _DurableOldFile(
                    path=path,
                    exists=snapshot.exists,
                    parent_identity=snapshot.parent_identity,
                    identity=snapshot.identity,
                    size=snapshot.size,
                    digest=snapshot.digest,
                )
            )
        writes: list[_DurableWrite] = []
        for path, staged in sorted(transaction.staged.items()):
            snapshot = snapshots[path]
            if snapshot.parent_identity is None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            payload = transaction.expected_writes[path]
            writes.append(
                _DurableWrite(
                    path=path,
                    stage_name=staged.name,
                    parent_identity=snapshot.parent_identity,
                    identity=staged.identity,
                    size=len(payload),
                    digest=staged.digest,
                )
            )
        manifest_write = next((item for item in writes if item.path == manifest_path), None)
        if manifest_write is None:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        old_manifest = snapshots[manifest_path]
        if old_manifest.exists != (previous is not None) or (
            previous is not None and old_manifest.digest is None
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        transaction.journal = _TransactionJournal(
            transaction_name=transaction.name,
            vault_identity=anchor.identity,
            transaction_identity=transaction.identity,
            owner_identity=transaction.owner_identity,
            stage_identity=transaction.stage_identity,
            backup_identity=transaction.backup_identity,
            manifest_path=manifest_path,
            old_manifest_checksum=None if previous is None else previous.checksum,
            new_manifest_checksum=rendered.manifest.checksum,
            old_manifest_digest=old_manifest.digest,
            new_manifest_digest=manifest_write.digest,
            old_files=tuple(old_files),
            writes=tuple(writes),
        )
        self._persist_transaction_phase(transaction, _TransactionPhase.PREPARED)

    def _persist_transaction_phase(
        self, transaction: _Transaction, phase: _TransactionPhase
    ) -> None:
        journal = transaction.journal
        if journal is None:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        transition_allowed = (
            transaction.phase is None
            and phase is _TransactionPhase.PREPARED
            or transaction.phase is _TransactionPhase.PREPARED
            and phase in {_TransactionPhase.MANIFEST_PENDING, _TransactionPhase.ROLLED_BACK}
            or transaction.phase is _TransactionPhase.MANIFEST_PENDING
            and phase in {_TransactionPhase.COMMITTED, _TransactionPhase.ROLLED_BACK}
        )
        if not transition_allowed:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        record_name = _state_record_name(phase)
        payload = _journal_bytes(journal, phase)
        if len(payload) > _MAX_TRANSACTION_STATE_BYTES:
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        descriptor: int | None = None
        identity: _Identity | None = None
        try:
            descriptor = os.open(record_name, _WRITE_FLAGS, 0o600, dir_fd=transaction.fd)
            identity = _identity(os.fstat(descriptor))
            transaction.state_records[record_name] = identity
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short transaction state write")
                offset += written
            _fsync_file(descriptor)
            _fsync_directory(transaction.fd)
            transaction.phase = phase
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

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
                self._persist_transaction_phase(transaction, _TransactionPhase.MANIFEST_PENDING)
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
        if parent is None:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        try:
            if _identity(os.fstat(parent)) != expected.parent_identity:
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
                backup.status = _BackupStatus.NO_MOVE_CONFLICT
                self._forget_moved(transaction, transaction.backup_fd, backup_name)
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            backup.status = _BackupStatus.MOVED
            moved_snapshot = self._snapshot_in_directory(transaction.backup_fd, backup_name)
            if (
                moved_snapshot.identity != backup.identity
                or moved_snapshot.size != backup.size
                or moved_snapshot.digest != backup.digest
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            backup.status = _BackupStatus.VERIFIED
            _fsync_directory(parent)
            _fsync_directory(transaction.backup_fd)
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
            _fsync_file(descriptor)
            _fsync_directory(transaction.fd)
            recovery.durable = True
        finally:
            if descriptor is not None:
                with suppress(OSError):
                    os.close(descriptor)

    def _forget_moved(self, transaction: _Transaction, directory_fd: int, moved_name: str) -> bool:
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
                return False
            _fsync_directory(transaction.fd)
            transaction.recoveries.pop(key, None)
        transaction.protected_entries.discard(key)
        return key not in transaction.recoveries and key not in transaction.protected_entries

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
        _fsync_directory(directory_fd)
        _fsync_directory(parent_fd)
        self._forget_moved(transaction, directory_fd, moved_name)
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
            _fsync_directory(parent)
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
                if removed:
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
                if restored:
                    break
            if not restored:
                safe = False
                if backup.status is not _BackupStatus.NO_MOVE_CONFLICT:
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
        if backup.status is _BackupStatus.NO_MOVE_CONFLICT:
            if not self._forget_moved(transaction, transaction.backup_fd, backup.name):
                return False
            conflict_visible: _FileSnapshot | None = None
            with suppress(ForgeException, OSError):
                conflict_visible = self._read_snapshot(anchor, backup.path)
            return conflict_visible is not None and self._snapshot_matches_backup(
                conflict_visible, backup
            )
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
                self._forget_moved(transaction, transaction.backup_fd, backup.name)
            elif destination_is_old:
                backup.status = _BackupStatus.RESTORED
            else:
                backup.status = _BackupStatus.PROTECTED
                return False
            return True
        if visible.exists or not destination_is_old:
            if (
                not visible.exists
                and destination_exists
                and backup.status in {_BackupStatus.MOVED, _BackupStatus.VERIFIED}
            ):
                self._restore_unverified_backup_entry(anchor, transaction, backup)
            backup.status = _BackupStatus.PROTECTED
            return False
        if backup.status is _BackupStatus.INTENT:
            backup.status = _BackupStatus.MOVED
        return self._restore_backup(anchor, transaction, backup)

    def _restore_unverified_backup_entry(
        self,
        anchor: _VaultAnchor,
        transaction: _Transaction,
        backup: _BackupFile,
    ) -> bool:
        if backup.status not in {_BackupStatus.MOVED, _BackupStatus.VERIFIED}:
            return False
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
        if published.status in {_PublishedStatus.ABSENT, _PublishedStatus.REMOVED}:
            return True
        parent_path = published.parent_path
        name = published.name
        parent = self._open_directory(anchor, parent_path, create=False)
        if parent is None:
            return False
        try:
            if _identity(os.fstat(parent)) != published.parent_identity:
                return False
            if published.rollback_name is not None:
                if published.rollback_status is _RollbackMoveStatus.MOVED:
                    settled = self._settle_published_move(transaction, published, parent)
                    if settled is not None:
                        return settled
                elif published.rollback_status is _RollbackMoveStatus.AMBIGUOUS:
                    return False
                elif published.rollback_status is _RollbackMoveStatus.NO_MOVE_CONFLICT:
                    if not self._forget_moved(transaction, transaction.fd, published.rollback_name):
                        return False
                    published.rollback_name = None
                    published.rollback_status = None
            source_state = self._published_entry_state(parent, name, published)
            if source_state == "absent":
                published.status = _PublishedStatus.ABSENT
                return True
            if source_state != "expected":
                published.status = _PublishedStatus.COMPETITOR
                return False
            rollback_name = f"r{len(transaction.protected_entries):06d}-{uuid4().hex}"
            published.rollback_name = rollback_name
            published.rollback_status = _RollbackMoveStatus.INTENT
            self._protect_moved(
                transaction,
                transaction.fd,
                "transaction",
                rollback_name,
                published.path,
            )
            try:
                moved = _rename_noreplace(
                    name,
                    rollback_name,
                    source_fd=parent,
                    destination_fd=transaction.fd,
                )
            except BaseException as rename_exc:
                source_state = self._published_entry_state(parent, name, published)
                if source_state == "expected":
                    published.rollback_status = _RollbackMoveStatus.NO_MOVE_CONFLICT
                    if self._forget_moved(transaction, transaction.fd, rollback_name):
                        published.rollback_name = None
                    raise rename_exc from None
                if source_state == "absent" and self._rollback_destination_matches(
                    transaction, rollback_name, published
                ):
                    published.rollback_status = _RollbackMoveStatus.MOVED
                    settled = self._settle_published_move(transaction, published, parent)
                    if settled:
                        if not isinstance(rename_exc, Exception):
                            raise rename_exc from None
                        return True
                published.rollback_status = _RollbackMoveStatus.AMBIGUOUS
                raise rename_exc from None
            if not moved:
                published.rollback_status = _RollbackMoveStatus.NO_MOVE_CONFLICT
                if not self._forget_moved(transaction, transaction.fd, rollback_name):
                    return False
                published.rollback_name = None
                published.rollback_status = None
                source_state = self._published_entry_state(parent, name, published)
                if source_state == "absent":
                    published.status = _PublishedStatus.ABSENT
                    return True
                if source_state == "expected":
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                published.status = _PublishedStatus.COMPETITOR
                return False
            published.rollback_status = _RollbackMoveStatus.MOVED
            settled = self._settle_published_move(transaction, published, parent)
            return bool(settled)
        except FileNotFoundError:
            if published.rollback_name is not None:
                self._forget_moved(transaction, transaction.fd, published.rollback_name)
                published.rollback_name = None
                published.rollback_status = _RollbackMoveStatus.NO_MOVE_CONFLICT
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
        if published.rollback_status is not _RollbackMoveStatus.MOVED:
            return False
        if not self._entry_exists(transaction.fd, rollback_name):
            self._forget_moved(transaction, transaction.fd, rollback_name)
            published.rollback_name = None
            published.rollback_status = _RollbackMoveStatus.AMBIGUOUS
            source_state = self._published_entry_state(parent, published.name, published)
            if source_state == "absent":
                published.status = _PublishedStatus.ABSENT
                return True
            return None if source_state == "expected" else False
        removed = self._discard_moved_published(transaction, rollback_name, published)
        if removed:
            _fsync_directory(transaction.fd)
            _fsync_directory(parent)
            published.status = _PublishedStatus.REMOVED
            published.rollback_name = None
            published.rollback_status = _RollbackMoveStatus.MOVED
            return True
        published.rollback_status = _RollbackMoveStatus.AMBIGUOUS
        published.status = _PublishedStatus.COMPETITOR
        return False

    def _published_entry_state(
        self, directory_fd: int, name: str, published: _PublishedFile
    ) -> str:
        try:
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "unknown"
        if not stat.S_ISREG(status.st_mode):
            return "other"
        try:
            snapshot = self._snapshot_in_directory(directory_fd, name)
        except FileNotFoundError:
            return "absent"
        except (ForgeException, OSError):
            return "unknown"
        expected = published.data
        if (
            snapshot.identity == published.identity
            and snapshot.size == published.size
            and snapshot.digest == published.digest
            and snapshot.data == expected
        ):
            return "expected"
        return "other"

    def _rollback_destination_matches(
        self,
        transaction: _Transaction,
        rollback_name: str,
        published: _PublishedFile,
    ) -> bool:
        try:
            snapshot = self._snapshot_in_directory(transaction.fd, rollback_name)
        except (FileNotFoundError, ForgeException, OSError):
            return False
        return (
            snapshot.identity == published.identity
            and snapshot.size == published.size
            and snapshot.digest == published.digest
            and snapshot.data == published.data
        )

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
        _fsync_directory(transaction.fd)
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
            if parent is None:
                return False
            try:
                if _identity(os.fstat(parent)) != backup.parent_identity:
                    return False
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
                    and visible.data == stored.data
                )
                if not restored:
                    self._unlink_named_identity(parent, name, backup.identity)
                    return False
                backup.status = _BackupStatus.RESTORED
                _fsync_directory(parent)
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
            removed = self._unlink_if_identity(
                transaction, transaction.stage_fd, staged.name, staged.identity
            )
            if removed:
                _fsync_directory(transaction.stage_fd)
        for backup in transaction.backups:
            removed = self._unlink_identity_without_protection(
                transaction.backup_fd,
                backup.name,
                backup.identity,
            )
            if removed:
                _fsync_directory(transaction.backup_fd)
                self._forget_moved(transaction, transaction.backup_fd, backup.name)
        _fsync_directory(transaction.stage_fd)
        _fsync_directory(transaction.backup_fd)

    def _cleanup_rolled_back(self, transaction: _Transaction) -> None:
        for staged in transaction.staged.values():
            removed = self._unlink_if_identity(
                transaction, transaction.stage_fd, staged.name, staged.identity
            )
            if removed:
                _fsync_directory(transaction.stage_fd)
        for backup in transaction.backups:
            if backup.status is not _BackupStatus.RESTORED:
                continue
            removed = self._unlink_identity_without_protection(
                transaction.backup_fd,
                backup.name,
                backup.identity,
            )
            if removed:
                _fsync_directory(transaction.backup_fd)
                self._forget_moved(transaction, transaction.backup_fd, backup.name)
        _fsync_directory(transaction.stage_fd)
        _fsync_directory(transaction.backup_fd)

    def _cleanup_failed(self, transaction: _Transaction) -> None:
        with suppress(OSError, ForgeException):
            for staged in transaction.staged.values():
                self._unlink_if_identity(
                    transaction, transaction.stage_fd, staged.name, staged.identity
                )
            for backup in transaction.backups:
                if backup.status is _BackupStatus.RESTORED:
                    removed = self._unlink_identity_without_protection(
                        transaction.backup_fd,
                        backup.name,
                        backup.identity,
                    )
                    if removed:
                        _fsync_directory(transaction.backup_fd)
                        self._forget_moved(transaction, transaction.backup_fd, backup.name)
                elif backup.status is _BackupStatus.NO_MOVE:
                    self._unlink_if_identity(
                        transaction,
                        transaction.backup_fd,
                        backup.name,
                        backup.identity,
                    )
            _fsync_directory(transaction.stage_fd)
            _fsync_directory(transaction.backup_fd)

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
        for name, identity in tuple(transaction.state_records.items()):
            if self._unlink_if_identity(transaction, transaction.fd, name, identity):
                transaction.state_records.pop(name, None)
        if transaction.state_records:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        _fsync_directory(transaction.fd)
        self._rmdir_if_identity(
            transaction.fd,
            "stage",
            transaction.stage_identity,
        )
        self._rmdir_if_identity(
            transaction.fd,
            "backup",
            transaction.backup_identity,
        )
        _fsync_directory(transaction.fd)
        if not self._unlink_if_identity(
            transaction,
            transaction.fd,
            _OWNER_NAME,
            transaction.owner_identity,
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        _fsync_directory(transaction.fd)
        self._rmdir_if_identity(
            transaction.parent_fd,
            transaction.name,
            transaction.identity,
        )
        _fsync_directory(transaction.parent_fd)
        pending_signal: BaseException | None = None
        for descriptor in (
            transaction.stage_fd,
            transaction.backup_fd,
            transaction.fd,
            transaction.owner_fd,
            transaction.parent_fd,
        ):
            try:
                os.close(descriptor)
            except OSError:
                continue
            except BaseException as exc:
                if pending_signal is None:
                    pending_signal = exc
        transaction.closed = True
        if pending_signal is not None:
            raise pending_signal

    @staticmethod
    def _rmdir_if_identity(
        parent_fd: int,
        name: str,
        expected: _Identity,
    ) -> None:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(current.st_mode) or _identity(current) != expected:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        os.rmdir(name, dir_fd=parent_fd)

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
