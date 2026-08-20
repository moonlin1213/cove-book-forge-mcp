"""Guarded complete-generation publication for canonical Agent Skills."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from cove_book_forge.config import SkillOutputConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs.skill_managed import (
    plan_skill_update,
    validate_rendered_skill,
    validate_skill_bundle,
)
from cove_book_forge.outputs.skill_models import (
    AgentSkillManifest,
    RenderedAgentSkill,
    SkillPublisherReceipt,
)
from cove_book_forge.path_safety import validate_relative_path

_O_DIRECTORY: Final = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK: Final = getattr(os, "O_NONBLOCK", 0)
_DIR_FLAGS: Final = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW
_READ_FLAGS: Final = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW
_MANAGEMENT: Final = ".cove-book-forge"
_OWNER: Final = ".owner.json"
_CONTENT: Final = "content"
_MAX_FILE_BYTES: Final = 8 * 1024 * 1024
_MAX_OWNER_BYTES: Final = 4 * 1024 * 1024
_MAX_TOTAL_TREE_BYTES: Final = 64 * 1024 * 1024
_MAX_TREE_FILES: Final = 5_011
_MAX_TREE_ENTRIES: Final = 10_024
_MAX_TRANSACTION_COUNT: Final = 64
_MAX_ACTIVATION_ENTRIES: Final = 128
_MAX_QUARANTINE_ENTRIES: Final = 128
_READ_CHUNK: Final = 1024 * 1024
_TX_NAME = re.compile(r"^tx-([0-9a-f]{32})$")
_GENERATION_NAME = re.compile(r"^gen-([0-9a-f]{64})$")
_ACTIVATION_NAME = re.compile(r"^activate-([0-9a-f]{32})$")
_QUARANTINE_NAME = re.compile(r"^q-([0-9a-f]{32})$")
_QUARANTINE_STAGE_NAME = re.compile(r"^stage-([0-9a-f]{32})$")
_QUARANTINE_CLOSING_NAME = re.compile(r"^closing-(q-[0-9a-f]{32})\.json$")
_STAGE_INTENT_NAME = re.compile(r"^stage-intent-([0-9a-f]{32})\.json$")
_STAGE_READY_NAME = re.compile(r"^stage-ready-([0-9a-f]{32})\.json$")
_CLOSING_INTENT_NAME = re.compile(r"^closing-intent-(q-[0-9a-f]{32})\.json$")
_STAGE_INTENT_TEMP_NAME = re.compile(r"^\.stage-intent-([0-9a-f]{32})\.tmp$")
_STAGE_READY_TEMP_NAME = re.compile(r"^\.stage-ready-([0-9a-f]{32})\.tmp$")
_CLOSING_INTENT_TEMP_NAME = re.compile(r"^\.closing-intent-(q-[0-9a-f]{32})\.tmp$")
_CLOSING_PARTIAL_NAME = re.compile(r"^\.closing-(q-[0-9a-f]{32})\.partial$")
_DELETE_INTENT_NAME = re.compile(r"^delete-intent-([0-9a-f]{32})\.json$")
_DELETE_INTENT_TEMP_NAME = re.compile(r"^\.delete-intent-([0-9a-f]{32})\.tmp$")
_DELETE_SLOT_NAME = re.compile(r"^\.delete-([0-9a-f]{32})$")
_RETIRED_RECORD_NAME = re.compile(
    r"^retired-(?:stage-intent|stage-ready|closing-intent|delete-intent)-[a-z0-9-]+\.json$"
)
_TARGET = re.compile(r"^\.cove-book-forge/generations/([0-9a-f]{16})/gen-([0-9a-f]{64})/content$")
_ROOT_OWNER_BYTES: Final = b'{"owner":"cove-book-forge-canonical-skills","schema":1}'
_SECURE_PRIMITIVES: Final = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and all(
        function in os.supports_dir_fd
        for function in (os.open, os.mkdir, os.stat, os.rename, os.unlink, os.rmdir)
    )
)

_Identity = tuple[int, int]


def _error(code: ForgeErrorCode) -> ForgeException:
    return ForgeException(code, "canonical Skill publication failed")


def _identity(status: os.stat_result) -> _Identity:
    return status.st_dev, status.st_ino


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _checksummed_record(payload: Mapping[str, object]) -> bytes:
    unsigned = dict(payload)
    if "checksum" in unsigned:
        raise ValueError("record payload already contains a checksum")
    checksum = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    return _canonical_json({**unsigned, "checksum": checksum})


def _load_checksummed_record(data: bytes) -> dict[str, Any]:
    record = _load_unique_json(data)
    checksum = record.get("checksum")
    unsigned = {key: value for key, value in record.items() if key != "checksum"}
    if (
        not isinstance(checksum, str)
        or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        or hashlib.sha256(_canonical_json(unsigned)).hexdigest() != checksum
    ):
        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
    return record


def _load_unique_json(data: bytes, *, max_bytes: int = _MAX_OWNER_BYTES) -> dict[str, Any]:
    if len(data) > max_bytes:
        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except Exception:
        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
    if not isinstance(value, dict) or data != _canonical_json(value):
        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
    return value


def _fsync_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
            raise


def _read_file(directory_fd: int, name: str, *, max_bytes: int) -> tuple[bytes, _Identity]:
    before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
    descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _identity(opened) != _identity(before)
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_READ_CHUNK, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        after = os.fstat(descriptor)
        if _identity(after) != _identity(opened) or after.st_nlink != 1 or after.st_size != total:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        return b"".join(chunks), _identity(opened)
    finally:
        os.close(descriptor)


def _write_file(directory_fd: int, name: str, payload: bytes) -> _Identity:
    descriptor = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError(errno.EIO, "short write")
            written += count
        os.fsync(descriptor)
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        return _identity(status)
    finally:
        os.close(descriptor)


def _bounded_names(descriptor: int, limit: int) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            if len(names) >= limit:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            names.append(entry.name)
    return tuple(names)


def _open_directory(parent_fd: int, name: str) -> tuple[int, _Identity]:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
    descriptor = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(before):
        os.close(descriptor)
        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
    return descriptor, _identity(opened)


def _create_or_open_directory(parent_fd: int, name: str) -> tuple[int, _Identity, bool]:
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
        _fsync_directory(parent_fd)
    except FileExistsError:
        pass
    descriptor, identity = _open_directory(parent_fd, name)
    return descriptor, identity, created


def _open_absolute_directory(path: Path) -> int:
    descriptor: int | None = None
    try:
        descriptor = os.open(path.anchor, _DIR_FLAGS)
        for component in path.parts[1:]:
            child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if descriptor is None or not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        return descriptor
    except FileNotFoundError:
        if descriptor is not None:
            os.close(descriptor)
        raise _error(ForgeErrorCode.OUTPUT_NOT_CONFIGURED) from None
    except ForgeException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    except (OSError, RuntimeError, ValueError):
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise _error(ForgeErrorCode.PATH_NOT_ALLOWED) from None


def _is_broad(path: Path) -> bool:
    try:
        home = Path.home().resolve(strict=True)
        current = Path.cwd().resolve(strict=True)
    except (OSError, RuntimeError):
        raise _error(ForgeErrorCode.PATH_NOT_ALLOWED) from None
    return path == Path(path.anchor) or path in {home, current, *home.parents, *current.parents}


@dataclass
class _RootAnchor:
    path: Path
    descriptor: int
    identity: _Identity

    @classmethod
    def capture(cls, config: SkillOutputConfig) -> _RootAnchor:
        if not config.enabled or config.canonical_path is None:
            raise _error(ForgeErrorCode.OUTPUT_NOT_CONFIGURED)
        if not _SECURE_PRIMITIVES:
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        path = config.canonical_path
        raw = str(path)
        if (
            "\x00" in raw
            or not path.is_absolute()
            or Path(os.path.abspath(path)) != path
            or _is_broad(path)
        ):
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        descriptor = _open_absolute_directory(path)
        identity = _identity(os.fstat(descriptor))
        anchor = cls(path, descriptor, identity)
        try:
            anchor.verify()
        except BaseException:
            os.close(descriptor)
            raise
        return anchor

    def verify(self) -> None:
        if _identity(os.fstat(self.descriptor)) != self.identity:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        current = _open_absolute_directory(self.path)
        try:
            if _identity(os.fstat(current)) != self.identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        finally:
            os.close(current)

    def close(self) -> None:
        os.close(self.descriptor)


@dataclass
class _Management:
    root: int
    generations: int
    transactions: int
    activations: int
    state: int
    quarantine: int
    root_link: _DirectoryLink
    generation_link: _DirectoryLink
    transaction_link: _DirectoryLink
    activation_link: _DirectoryLink
    state_link: _DirectoryLink
    quarantine_link: _DirectoryLink

    def verify(self, anchor: _RootAnchor) -> None:
        anchor.verify()
        if _identity(os.fstat(anchor.descriptor)) != self.root_link.parent_identity:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        root, root_identity = _open_directory(anchor.descriptor, self.root_link.name)
        try:
            if root_identity != self.root_link.identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            for link in (
                self.generation_link,
                self.transaction_link,
                self.activation_link,
                self.state_link,
                self.quarantine_link,
            ):
                if link.parent_identity != root_identity:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                child, child_identity = _open_directory(root, link.name)
                try:
                    if child_identity != link.identity:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                finally:
                    os.close(child)
        finally:
            os.close(root)
        for descriptor, link in (
            (self.root, self.root_link),
            (self.generations, self.generation_link),
            (self.transactions, self.transaction_link),
            (self.activations, self.activation_link),
            (self.state, self.state_link),
            (self.quarantine, self.quarantine_link),
        ):
            if _identity(os.fstat(descriptor)) != link.identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def open_current_generations(self, anchor: _RootAnchor) -> int:
        self.verify(anchor)
        root, root_identity = _open_directory(anchor.descriptor, self.root_link.name)
        try:
            if root_identity != self.root_link.identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            generations, generation_identity = _open_directory(root, self.generation_link.name)
        finally:
            os.close(root)
        if generation_identity != self.generation_link.identity:
            os.close(generations)
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        return generations

    def close(self) -> None:
        for descriptor in (
            self.quarantine,
            self.state,
            self.activations,
            self.transactions,
            self.generations,
            self.root,
        ):
            with suppress(OSError):
                os.close(descriptor)


@dataclass(frozen=True)
class _ActiveGeneration:
    target: str
    pointer_identity: _Identity
    manifest: AgentSkillManifest
    files: Mapping[str, bytes]


@dataclass(frozen=True)
class _DirectoryLink:
    parent_identity: _Identity
    name: str
    identity: _Identity


@dataclass(frozen=True)
class _RawEntry:
    mode_type: int
    identity: _Identity
    target: str | None


@dataclass(frozen=True)
class _PublicationState:
    book_key: str
    skill_slug: str
    current_target: str
    previous_target: str | None


@dataclass(frozen=True)
class _AuditEntry:
    directory: bool
    identity: _Identity
    nlink: int | None = None
    size: int | None = None
    digest: str | None = None


def _rename_noreplace(
    source: str, destination: str, *, source_fd: int, destination_fd: int
) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            function = libc.renameatx_np
        except AttributeError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        flags = 0x00000004
    elif sys.platform.startswith("linux"):
        try:
            function = libc.renameat2
        except AttributeError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        flags = 1
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
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        flags,
    )
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        return False
    raise OSError(error_number, os.strerror(error_number))


def _rename_exchange(source: str, destination: str, *, source_fd: int, destination_fd: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        try:
            function = libc.renameatx_np
        except AttributeError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        flags = 0x00000002
    elif sys.platform.startswith("linux"):
        try:
            function = libc.renameat2
        except AttributeError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        flags = 2
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
        os.fsencode(source),
        destination_fd,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _raw_entry(directory_fd: int, name: str) -> _RawEntry | None:
    """Return a no-follow entry snapshot without interpreting or rejecting its kind."""
    try:
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError:
        return None
    target: str | None = None
    if stat.S_ISLNK(entry.st_mode):
        try:
            target = os.readlink(name, dir_fd=directory_fd)
        except OSError:
            target = None
    return _RawEntry(stat.S_IFMT(entry.st_mode), _identity(entry), target)


class CanonicalSkillPublisher:
    """Publish immutable complete generations through one relative atomic pointer."""

    def __init__(self, config: SkillOutputConfig) -> None:
        self._config = config

    def _checkpoint(self, phase: str) -> None:
        del phase

    def publish(self, render: RenderedAgentSkill) -> SkillPublisherReceipt:
        try:
            return self._publish(render)
        except ForgeException:
            raise
        except PermissionError:
            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
        except Exception:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None

    def _publish(self, render: RenderedAgentSkill) -> SkillPublisherReceipt:
        validate_rendered_skill(render)
        anchor = _RootAnchor.capture(self._config)
        management: _Management | None = None
        try:
            management = self._management(anchor)
            for directory_fd in (
                management.transactions,
                management.activations,
                management.state,
            ):
                self._recover_delete_transitions(directory_fd)
            self._recover_transactions(management)
            self._recover_activations(anchor, management)
            self._recover_quarantines(management)
            anchor.verify()
            active = self._active_generation(anchor, management, render.skill_slug)
            if active is None:
                plan = plan_skill_update(None, {}, render)
            else:
                plan = plan_skill_update(active.manifest, active.files, render)
            if plan.unchanged:
                assert active is not None
                anchor.verify()
                self._cleanup_generations(management, render.manifest, active.target)
                self._checkpoint("hierarchy:before-return")
                management.verify(anchor)
                return SkillPublisherReceipt(render, render.skill_slug, (), True)

            target = self._ensure_generation(management, render, plan.complete_files)
            self._checkpoint("hierarchy:after-stage")
            management.verify(anchor)
            self._activate(anchor, management, render, target, active)
            self._checkpoint("hierarchy:after-cas")
            management.verify(anchor)
            self._checkpoint("manifest:switch")
            visible = self._active_generation(anchor, management, render.skill_slug)
            if visible is None or visible.manifest != render.manifest or visible.target != target:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._checkpoint("cleanup:start")
            self._cleanup_generations(management, render.manifest, target)
            self._checkpoint("hierarchy:before-return")
            management.verify(anchor)
            return SkillPublisherReceipt(
                render,
                render.skill_slug,
                plan.changed_paths,
                False,
            )
        finally:
            if management is not None:
                management.close()
            anchor.close()

    def _management(self, anchor: _RootAnchor) -> _Management:
        root, _, created = _create_or_open_directory(anchor.descriptor, _MANAGEMENT)
        children: list[int] = []
        try:
            names = set(_bounded_names(root, 8))
            if created:
                _write_file(root, _OWNER, _ROOT_OWNER_BYTES)
                _fsync_directory(root)
                names.add(_OWNER)
            owner, _ = _read_file(root, _OWNER, max_bytes=len(_ROOT_OWNER_BYTES))
            if owner != _ROOT_OWNER_BYTES:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            allowed = {
                _OWNER,
                "generations",
                "transactions",
                "activations",
                "state",
                "quarantine",
            }
            if not names <= allowed:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            root_identity = _identity(os.fstat(root))
            generations, generation_identity, _ = _create_or_open_directory(root, "generations")
            children.append(generations)
            transactions, transaction_identity, _ = _create_or_open_directory(root, "transactions")
            children.append(transactions)
            activations, activation_identity, _ = _create_or_open_directory(root, "activations")
            children.append(activations)
            state, state_identity, _ = _create_or_open_directory(root, "state")
            children.append(state)
            quarantine, quarantine_identity, _ = _create_or_open_directory(root, "quarantine")
            children.append(quarantine)
            management = _Management(
                root,
                generations,
                transactions,
                activations,
                state,
                quarantine,
                _DirectoryLink(anchor.identity, _MANAGEMENT, root_identity),
                _DirectoryLink(root_identity, "generations", generation_identity),
                _DirectoryLink(root_identity, "transactions", transaction_identity),
                _DirectoryLink(root_identity, "activations", activation_identity),
                _DirectoryLink(root_identity, "state", state_identity),
                _DirectoryLink(root_identity, "quarantine", quarantine_identity),
            )
            management.verify(anchor)
            return management
        except BaseException:
            for descriptor in reversed(children):
                with suppress(OSError):
                    os.close(descriptor)
            os.close(root)
            raise

    def _read_tree(self, root_fd: int) -> tuple[dict[str, bytes], dict[str, str]]:
        files: dict[str, bytes] = {}
        kinds: dict[str, str] = {}
        entry_count = 0
        total_bytes = 0

        def visit(directory_fd: int, prefix: str) -> None:
            nonlocal entry_count, total_bytes
            for name in _bounded_names(directory_fd, _MAX_TREE_ENTRIES - entry_count):
                entry_count += 1
                if entry_count > _MAX_TREE_ENTRIES:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                path = f"{prefix}/{name}" if prefix else name
                try:
                    validate_relative_path(path)
                except ValueError:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
                entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(entry.st_mode):
                    child, _ = _open_directory(directory_fd, name)
                    try:
                        visit(child, path)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(entry.st_mode):
                    if len(files) >= _MAX_TREE_FILES or entry.st_nlink != 1:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    remaining = _MAX_TOTAL_TREE_BYTES - total_bytes
                    payload, _ = _read_file(
                        directory_fd,
                        name,
                        max_bytes=min(_MAX_FILE_BYTES, remaining),
                    )
                    total_bytes += len(payload)
                    if total_bytes > _MAX_TOTAL_TREE_BYTES:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    files[path] = payload
                    kinds[path] = "file"
                else:
                    kinds[path] = "symlink" if stat.S_ISLNK(entry.st_mode) else "other"
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

        visit(root_fd, "")
        return files, kinds

    def _validate_generation(
        self,
        book_fd: int,
        generation_name: str,
        *,
        expected_book_key: str | None = None,
        expected_checksum: str | None = None,
        expected_slug: str | None = None,
    ) -> tuple[AgentSkillManifest, dict[str, bytes], _Identity]:
        match = _GENERATION_NAME.fullmatch(generation_name)
        if match is None:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        generation_fd, generation_identity = _open_directory(book_fd, generation_name)
        try:
            names = set(_bounded_names(generation_fd, 3))
            if names != {_OWNER, _CONTENT}:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            owner_bytes, _ = _read_file(generation_fd, _OWNER, max_bytes=_MAX_OWNER_BYTES)
            owner = _load_unique_json(owner_bytes)
            content_fd, _ = _open_directory(generation_fd, _CONTENT)
            try:
                files, kinds = self._read_tree(content_fd)
            finally:
                os.close(content_fd)
            manifest = validate_skill_bundle(files, entry_kinds=kinds)
            owner_files = owner.get("files")
            expected_hashes = {
                path: hashlib.sha256(payload).hexdigest() for path, payload in sorted(files.items())
            }
            if (
                set(owner)
                != {
                    "book_key",
                    "checksum",
                    "files",
                    "owner",
                    "schema",
                    "skill_slug",
                    "tx_id",
                }
                or owner.get("owner") != "cove-book-forge-skill-transaction"
                or owner.get("schema") != 1
                or owner.get("book_key") != manifest.book_key
                or owner.get("checksum") != manifest.checksum
                or owner.get("skill_slug") != manifest.skill_slug
                or not isinstance(owner.get("tx_id"), str)
                or re.fullmatch(r"[0-9a-f]{32}", owner["tx_id"]) is None
                or owner_files != expected_hashes
                or match.group(1) != manifest.checksum
                or expected_book_key is not None
                and manifest.book_key != expected_book_key
                or expected_checksum is not None
                and manifest.checksum != expected_checksum
                or expected_slug is not None
                and manifest.skill_slug != expected_slug
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            return manifest, files, generation_identity
        finally:
            os.close(generation_fd)

    def _target_parts(self, target: str) -> tuple[str, str]:
        match = _TARGET.fullmatch(target)
        if match is None or os.path.isabs(target) or ".." in Path(target).parts:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        try:
            validate_relative_path(target)
        except ValueError:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
        return match.group(1), f"gen-{match.group(2)}"

    def _validate_target(
        self,
        management: _Management,
        target: str,
        *,
        expected_slug: str | None = None,
        anchor: _RootAnchor | None = None,
    ) -> tuple[AgentSkillManifest, dict[str, bytes]]:
        book_key, generation_name = self._target_parts(target)
        generations_fd = (
            management.open_current_generations(anchor)
            if anchor is not None
            else os.dup(management.generations)
        )
        try:
            book_fd, _ = _open_directory(generations_fd, book_key)
            try:
                manifest, files, _ = self._validate_generation(
                    book_fd,
                    generation_name,
                    expected_book_key=book_key,
                    expected_slug=expected_slug,
                )
                return manifest, files
            finally:
                os.close(book_fd)
        finally:
            os.close(generations_fd)

    def _active_pointer(self, anchor: _RootAnchor, skill_slug: str) -> tuple[str, _Identity] | None:
        try:
            entry = os.stat(skill_slug, dir_fd=anchor.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISLNK(entry.st_mode):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        target = os.readlink(skill_slug, dir_fd=anchor.descriptor)
        after = os.stat(skill_slug, dir_fd=anchor.descriptor, follow_symlinks=False)
        if _identity(after) != _identity(entry) or not stat.S_ISLNK(after.st_mode):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        self._target_parts(target)
        return target, _identity(entry)

    def _active_generation(
        self, anchor: _RootAnchor, management: _Management, skill_slug: str
    ) -> _ActiveGeneration | None:
        pointer = self._active_pointer(anchor, skill_slug)
        if pointer is None:
            return None
        target, pointer_identity = pointer
        manifest, files = self._validate_target(
            management, target, expected_slug=skill_slug, anchor=anchor
        )
        anchor.verify()
        current = self._active_pointer(anchor, skill_slug)
        if current != pointer:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        management.verify(anchor)
        return _ActiveGeneration(target, pointer_identity, manifest, files)

    def _transaction_owner(
        self,
        tx_id: str,
        render: RenderedAgentSkill,
        complete_files: Mapping[str, bytes],
    ) -> dict[str, object]:
        return {
            "book_key": render.manifest.book_key,
            "checksum": render.manifest.checksum,
            "files": {
                path: hashlib.sha256(payload).hexdigest()
                for path, payload in sorted(complete_files.items())
            },
            "owner": "cove-book-forge-skill-transaction",
            "schema": 1,
            "skill_slug": render.skill_slug,
            "tx_id": tx_id,
        }

    def _stage_generation(
        self,
        management: _Management,
        render: RenderedAgentSkill,
        complete_files: Mapping[str, bytes],
    ) -> str:
        tx_id = uuid4().hex
        tx_name = f"tx-{tx_id}"
        os.mkdir(tx_name, 0o700, dir_fd=management.transactions)
        _fsync_directory(management.transactions)
        tx_fd, _ = _open_directory(management.transactions, tx_name)
        try:
            try:
                owner = self._transaction_owner(tx_id, render, complete_files)
                _write_file(tx_fd, _OWNER, _canonical_json(owner))
                os.mkdir(_CONTENT, 0o700, dir_fd=tx_fd)
                content_fd, _ = _open_directory(tx_fd, _CONTENT)
                try:
                    _fsync_directory(tx_fd)
                    self._checkpoint("stage:start")
                    checkpointed = False
                    for path, payload in sorted(complete_files.items()):
                        parent_fd = os.dup(content_fd)
                        try:
                            components = path.split("/")
                            for component in components[:-1]:
                                child, _, _ = _create_or_open_directory(parent_fd, component)
                                os.close(parent_fd)
                                parent_fd = child
                            _write_file(parent_fd, components[-1], payload)
                            _fsync_directory(parent_fd)
                        finally:
                            os.close(parent_fd)
                        if not checkpointed:
                            checkpointed = True
                            self._checkpoint("stage:file")
                    _fsync_directory(content_fd)
                finally:
                    os.close(content_fd)
                _fsync_directory(tx_fd)
                content_fd, _ = _open_directory(tx_fd, _CONTENT)
                try:
                    files, kinds = self._read_tree(content_fd)
                finally:
                    os.close(content_fd)
                if (
                    files != dict(complete_files)
                    or validate_skill_bundle(files, entry_kinds=kinds) != render.manifest
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            finally:
                os.close(tx_fd)
        except Exception:
            self._remove_owned_transaction(management, tx_name)
            raise
        return tx_name

    def _ensure_generation(
        self,
        management: _Management,
        render: RenderedAgentSkill,
        complete_files: Mapping[str, bytes],
    ) -> str:
        book_fd, _, _ = _create_or_open_directory(management.generations, render.manifest.book_key)
        generation_name = f"gen-{render.manifest.checksum}"
        target = (
            f"{_MANAGEMENT}/generations/{render.manifest.book_key}/{generation_name}/{_CONTENT}"
        )
        try:
            try:
                existing, files, _ = self._validate_generation(
                    book_fd,
                    generation_name,
                    expected_book_key=render.manifest.book_key,
                    expected_checksum=render.manifest.checksum,
                    expected_slug=render.skill_slug,
                )
            except FileNotFoundError:
                existing = None
                files = {}
            if existing is not None:
                if existing != render.manifest or files != dict(complete_files):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                return target

            tx_name = self._stage_generation(management, render, complete_files)
            try:
                moved = _rename_noreplace(
                    tx_name,
                    generation_name,
                    source_fd=management.transactions,
                    destination_fd=book_fd,
                )
                if not moved:
                    existing, files, _ = self._validate_generation(
                        book_fd,
                        generation_name,
                        expected_book_key=render.manifest.book_key,
                        expected_checksum=render.manifest.checksum,
                        expected_slug=render.skill_slug,
                    )
                    if existing != render.manifest or files != dict(complete_files):
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    self._remove_owned_transaction(management, tx_name)
                _fsync_directory(book_fd)
                _fsync_directory(management.transactions)
            except Exception:
                with suppress(Exception):
                    self._remove_owned_transaction(management, tx_name)
                raise
            self._validate_generation(
                book_fd,
                generation_name,
                expected_book_key=render.manifest.book_key,
                expected_checksum=render.manifest.checksum,
                expected_slug=render.skill_slug,
            )
            return target
        finally:
            os.close(book_fd)

    def _activation_marker(
        self,
        name: str,
        render: RenderedAgentSkill,
        new_target: str,
        old_target: str | None,
    ) -> dict[str, object]:
        return {
            "active_name": render.skill_slug,
            "book_key": render.manifest.book_key,
            "name": name,
            "new_target": new_target,
            "old_target": old_target,
            "owner": "cove-book-forge-skill-activation",
            "schema": 1,
        }

    def _state_payload(
        self,
        *,
        book_key: str,
        skill_slug: str,
        current_target: str,
        previous_target: str | None,
    ) -> bytes:
        payload = _canonical_json(
            {
                "book_key": book_key,
                "current_target": current_target,
                "owner": "cove-book-forge-skill-state",
                "previous_target": previous_target,
                "schema": 1,
                "skill_slug": skill_slug,
            }
        )
        return payload

    def _parse_state(self, payload: bytes, *, book_key: str, skill_slug: str) -> _PublicationState:
        state = _load_unique_json(payload)
        current_target = state.get("current_target")
        previous_target = state.get("previous_target")
        if (
            set(state)
            != {
                "book_key",
                "current_target",
                "owner",
                "previous_target",
                "schema",
                "skill_slug",
            }
            or state.get("owner") != "cove-book-forge-skill-state"
            or state.get("schema") != 1
            or state.get("book_key") != book_key
            or state.get("skill_slug") != skill_slug
            or not isinstance(current_target, str)
            or previous_target is not None
            and not isinstance(previous_target, str)
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        current_book, _ = self._target_parts(current_target)
        if current_book != book_key:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        if previous_target is not None:
            previous_book, _ = self._target_parts(previous_target)
            if previous_book != book_key or previous_target == current_target:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        return _PublicationState(book_key, skill_slug, current_target, previous_target)

    def _read_state(
        self, management: _Management, *, book_key: str, skill_slug: str
    ) -> tuple[_PublicationState, bytes, _Identity] | None:
        name = f"{book_key}.json"
        try:
            payload, identity = _read_file(management.state, name, max_bytes=_MAX_OWNER_BYTES)
        except FileNotFoundError:
            return None
        return (
            self._parse_state(payload, book_key=book_key, skill_slug=skill_slug),
            payload,
            identity,
        )

    def _write_state(
        self,
        management: _Management,
        *,
        book_key: str,
        skill_slug: str,
        current_target: str,
        previous_target: str | None,
        scratch_name: str,
    ) -> None:
        payload = self._state_payload(
            book_key=book_key,
            skill_slug=skill_slug,
            current_target=current_target,
            previous_target=previous_target,
        )
        self._parse_state(payload, book_key=book_key, skill_slug=skill_slug)
        name = f"{book_key}.json"
        scratch = f"{scratch_name}.state"
        previous = self._read_state(management, book_key=book_key, skill_slug=skill_slug)
        try:
            stale_payload, stale_identity = _read_file(
                management.state, scratch, max_bytes=_MAX_OWNER_BYTES
            )
        except FileNotFoundError:
            pass
        else:
            self._parse_state(stale_payload, book_key=book_key, skill_slug=skill_slug)
            if stale_payload != payload and (previous is None or stale_payload != previous[1]):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._unlink_identity(
                management.state,
                scratch,
                stale_identity,
                expected_digest=hashlib.sha256(stale_payload).hexdigest(),
                expected_size=len(stale_payload),
            )
            _fsync_directory(management.state)

        if previous is not None and previous[1] == payload:
            return
        new_identity = _write_file(management.state, scratch, payload)
        _fsync_directory(management.state)
        if previous is None:
            if not _rename_noreplace(
                scratch,
                name,
                source_fd=management.state,
                destination_fd=management.state,
            ):
                self._unlink_identity(
                    management.state,
                    scratch,
                    new_identity,
                    expected_digest=hashlib.sha256(payload).hexdigest(),
                    expected_size=len(payload),
                )
                _fsync_directory(management.state)
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        else:
            _, previous_payload, previous_identity = previous
            expected_new = _RawEntry(stat.S_IFREG, new_identity, None)
            expected_old = _RawEntry(stat.S_IFREG, previous_identity, None)
            _rename_exchange(
                scratch,
                name,
                source_fd=management.state,
                destination_fd=management.state,
            )
            try:
                _fsync_directory(management.state)
                if (
                    _raw_entry(management.state, name) != expected_new
                    or _raw_entry(management.state, scratch) != expected_old
                    or _read_file(management.state, name, max_bytes=_MAX_OWNER_BYTES)[0] != payload
                    or _read_file(management.state, scratch, max_bytes=_MAX_OWNER_BYTES)[0]
                    != previous_payload
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            except BaseException:
                if (
                    _raw_entry(management.state, name) == expected_new
                    and _raw_entry(management.state, scratch) is not None
                ):
                    _rename_exchange(
                        scratch,
                        name,
                        source_fd=management.state,
                        destination_fd=management.state,
                    )
                    _fsync_directory(management.state)
                raise
            self._unlink_identity(
                management.state,
                scratch,
                previous_identity,
                expected_digest=hashlib.sha256(previous_payload).hexdigest(),
                expected_size=len(previous_payload),
            )
        _fsync_directory(management.state)
        stored = self._read_state(management, book_key=book_key, skill_slug=skill_slug)
        if stored is None or stored[1] != payload:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _create_activation(
        self,
        management: _Management,
        render: RenderedAgentSkill,
        new_target: str,
        old_target: str | None,
    ) -> tuple[str, _Identity]:
        name = f"activate-{uuid4().hex}"
        marker = self._activation_marker(name, render, new_target, old_target)
        _write_file(management.activations, f"{name}.json", _canonical_json(marker))
        os.symlink(new_target, name, dir_fd=management.activations)
        entry = os.stat(name, dir_fd=management.activations, follow_symlinks=False)
        if not stat.S_ISLNK(entry.st_mode):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        _fsync_directory(management.activations)
        return name, _identity(entry)

    def _publish_record(
        self,
        directory_fd: int,
        *,
        temporary_name: str,
        record_name: str,
        payload: bytes,
    ) -> _Identity:
        temporary_identity = _write_file(directory_fd, temporary_name, payload)
        return self._finish_record_publication(
            directory_fd,
            temporary_name=temporary_name,
            record_name=record_name,
            payload=payload,
            temporary_identity=temporary_identity,
        )

    def _finish_record_publication(
        self,
        directory_fd: int,
        *,
        temporary_name: str,
        record_name: str,
        payload: bytes,
        temporary_identity: _Identity,
    ) -> _Identity:
        if not _rename_noreplace(
            temporary_name,
            record_name,
            source_fd=directory_fd,
            destination_fd=directory_fd,
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        _fsync_directory(directory_fd)
        stored, record_identity = _read_file(directory_fd, record_name, max_bytes=_MAX_OWNER_BYTES)
        if stored != payload or record_identity != temporary_identity:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        return record_identity

    def _retire_record(
        self,
        directory_fd: int,
        *,
        record_name: str,
        retired_name: str,
        payload: bytes,
        identity: _Identity,
    ) -> None:
        if not _rename_noreplace(
            record_name,
            retired_name,
            source_fd=directory_fd,
            destination_fd=directory_fd,
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        _fsync_directory(directory_fd)
        stored, retired_identity = _read_file(
            directory_fd, retired_name, max_bytes=_MAX_OWNER_BYTES
        )
        if stored != payload or retired_identity != identity:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        os.unlink(retired_name, dir_fd=directory_fd)
        _fsync_directory(directory_fd)

    def _recover_retired_records(self, directory_fd: int) -> None:
        for retired_name in _bounded_names(directory_fd, _MAX_TREE_ENTRIES):
            if _RETIRED_RECORD_NAME.fullmatch(retired_name) is None:
                continue
            payload, identity = _read_file(directory_fd, retired_name, max_bytes=_MAX_OWNER_BYTES)
            record = _load_checksummed_record(payload)
            if (
                record.get("retired_name") != retired_name
                or not isinstance(record.get("record_name"), str)
                or _raw_entry(directory_fd, record["record_name"]) is not None
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            stored, stored_identity = _read_file(directory_fd, retired_name, max_bytes=len(payload))
            if stored != payload or stored_identity != identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            os.unlink(retired_name, dir_fd=directory_fd)
            _fsync_directory(directory_fd)

    def _write_interrupted_file(
        self,
        directory_fd: int,
        *,
        partial_name: str,
        final_name: str,
        payload: bytes,
        checkpoint: str,
    ) -> _Identity:
        descriptor = os.open(partial_name, _CREATE_FLAGS, 0o600, dir_fd=directory_fd)
        try:
            split = max(1, len(payload) // 2)
            written = 0
            while written < split:
                count = os.write(descriptor, payload[written:split])
                if count <= 0:
                    raise OSError(errno.EIO, "short write")
                written += count
            self._checkpoint(checkpoint)
            while written < len(payload):
                count = os.write(descriptor, payload[written:])
                if count <= 0:
                    raise OSError(errno.EIO, "short write")
                written += count
            os.fsync(descriptor)
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_nlink != 1
                or status.st_size != len(payload)
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            partial_identity = _identity(status)
        finally:
            os.close(descriptor)
        if not _rename_noreplace(
            partial_name,
            final_name,
            source_fd=directory_fd,
            destination_fd=directory_fd,
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        _fsync_directory(directory_fd)
        stored, final_identity = _read_file(directory_fd, final_name, max_bytes=len(payload))
        if stored != payload or final_identity != partial_identity:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        return final_identity

    def _delete_intent_payload(
        self,
        *,
        record_name: str,
        retired_name: str,
        source_name: str,
        destination_name: str,
        status: os.stat_result,
        digest: str | None,
        target: str | None,
    ) -> bytes:
        return _checksummed_record(
            {
                "destination_name": destination_name,
                "digest": digest,
                "entry_dev": status.st_dev,
                "entry_ino": status.st_ino,
                "entry_type": stat.S_IFMT(status.st_mode),
                "nlink": status.st_nlink,
                "owner": "cove-book-forge-delete-intent",
                "record_name": record_name,
                "retired_name": retired_name,
                "schema": 1,
                "size": status.st_size,
                "source_name": source_name,
                "target": target,
            }
        )

    def _parse_delete_intent(
        self, payload: bytes, *, record_name: str
    ) -> tuple[str, str, str, _RawEntry, int, int, str | None]:
        record = _load_checksummed_record(payload)
        match = _DELETE_INTENT_NAME.fullmatch(record_name)
        expected_keys = {
            "checksum",
            "destination_name",
            "digest",
            "entry_dev",
            "entry_ino",
            "entry_type",
            "nlink",
            "owner",
            "record_name",
            "retired_name",
            "schema",
            "size",
            "source_name",
            "target",
        }
        identifier = match.group(1) if match is not None else ""
        source_name = record.get("source_name")
        destination_name = record.get("destination_name")
        retired_name = record.get("retired_name")
        entry_type = record.get("entry_type")
        digest = record.get("digest")
        target = record.get("target")
        if (
            match is None
            or set(record) != expected_keys
            or record.get("owner") != "cove-book-forge-delete-intent"
            or record.get("schema") != 1
            or record.get("record_name") != record_name
            or retired_name != f"retired-delete-intent-{identifier}.json"
            or destination_name != f".delete-{identifier}"
            or not isinstance(source_name, str)
            or source_name in {"", ".", ".."}
            or "/" in source_name
            or "\\" in source_name
            or entry_type not in {stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}
            or not isinstance(record.get("entry_dev"), int)
            or not isinstance(record.get("entry_ino"), int)
            or not isinstance(record.get("nlink"), int)
            or record["nlink"] < 1
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
            or entry_type == stat.S_IFREG
            and (
                record["nlink"] != 1
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or target is not None
            )
            or entry_type == stat.S_IFDIR
            and (digest is not None or target is not None)
            or entry_type == stat.S_IFLNK
            and (digest is not None or not isinstance(target, str))
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        assert isinstance(destination_name, str)
        assert isinstance(retired_name, str)
        assert isinstance(entry_type, int)
        return (
            source_name,
            destination_name,
            retired_name,
            _RawEntry(entry_type, (record["entry_dev"], record["entry_ino"]), target),
            record["nlink"],
            record["size"],
            digest,
        )

    def _verify_delete_record_association(
        self,
        directory_fd: int,
        *,
        source_name: str,
        destination_name: str,
        expected: _RawEntry,
        expected_nlink: int,
        expected_size: int,
        expected_digest: str | None,
    ) -> None:
        if (
            _raw_entry(directory_fd, source_name) != expected
            or _raw_entry(directory_fd, destination_name) is not None
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        status = os.stat(source_name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            _identity(status) != expected.identity
            or stat.S_IFMT(status.st_mode) != expected.mode_type
            or status.st_nlink != expected_nlink
            or status.st_size != expected_size
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        if expected.mode_type == stat.S_IFREG:
            payload, identity = _read_file(
                directory_fd,
                source_name,
                max_bytes=expected_size,
            )
            if (
                identity != expected.identity
                or expected_digest is None
                or hashlib.sha256(payload).hexdigest() != expected_digest
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        elif expected.mode_type == stat.S_IFDIR:
            child, identity = _open_directory(directory_fd, source_name)
            try:
                if identity != expected.identity or _bounded_names(child, 1):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            finally:
                os.close(child)

    def _recover_delete_record_temporaries(self, directory_fd: int) -> None:
        for temporary_name in sorted(_bounded_names(directory_fd, _MAX_TREE_ENTRIES)):
            match = _DELETE_INTENT_TEMP_NAME.fullmatch(temporary_name)
            if match is None:
                continue
            record_name = f"delete-intent-{match.group(1)}.json"
            payload, temporary_identity = _read_file(
                directory_fd,
                temporary_name,
                max_bytes=_MAX_OWNER_BYTES,
            )
            (
                source_name,
                destination_name,
                _,
                expected,
                expected_nlink,
                expected_size,
                expected_digest,
            ) = self._parse_delete_intent(payload, record_name=record_name)
            if _raw_entry(directory_fd, record_name) is not None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._verify_delete_record_association(
                directory_fd,
                source_name=source_name,
                destination_name=destination_name,
                expected=expected,
                expected_nlink=expected_nlink,
                expected_size=expected_size,
                expected_digest=expected_digest,
            )
            self._finish_record_publication(
                directory_fd,
                temporary_name=temporary_name,
                record_name=record_name,
                payload=payload,
                temporary_identity=temporary_identity,
            )

    def _delete_isolated_entry(
        self,
        directory_fd: int,
        name: str,
        expected: _RawEntry,
        *,
        expected_nlink: int,
        expected_size: int,
        expected_digest: str | None,
    ) -> None:
        moved = _raw_entry(directory_fd, name)
        if moved != expected:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        if expected.mode_type == stat.S_IFREG:
            payload, identity = _read_file(directory_fd, name, max_bytes=expected_size)
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                identity != expected.identity
                or status.st_nlink != expected_nlink
                or status.st_size != expected_size
                or expected_digest is None
                or hashlib.sha256(payload).hexdigest() != expected_digest
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            os.unlink(name, dir_fd=directory_fd)
        elif expected.mode_type == stat.S_IFLNK:
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if status.st_nlink != expected_nlink or status.st_size != expected_size:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            os.unlink(name, dir_fd=directory_fd)
        else:
            child, identity = _open_directory(directory_fd, name)
            try:
                status = os.fstat(child)
                if (
                    identity != expected.identity
                    or status.st_nlink != expected_nlink
                    or status.st_size != expected_size
                    or _bounded_names(child, 1)
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=directory_fd)
        _fsync_directory(directory_fd)

    def _recover_delete_transitions(self, directory_fd: int) -> None:
        self._recover_retired_records(directory_fd)
        self._recover_delete_record_temporaries(directory_fd)
        names = _bounded_names(directory_fd, _MAX_TREE_ENTRIES)
        for record_name in sorted(names):
            if _DELETE_INTENT_NAME.fullmatch(record_name) is None:
                continue
            payload, record_identity = _read_file(
                directory_fd, record_name, max_bytes=_MAX_OWNER_BYTES
            )
            (
                source_name,
                destination_name,
                retired_name,
                expected,
                expected_nlink,
                expected_size,
                expected_digest,
            ) = self._parse_delete_intent(payload, record_name=record_name)
            source = _raw_entry(directory_fd, source_name)
            destination = _raw_entry(directory_fd, destination_name)
            if source == expected and destination is None:
                self._verify_delete_record_association(
                    directory_fd,
                    source_name=source_name,
                    destination_name=destination_name,
                    expected=expected,
                    expected_nlink=expected_nlink,
                    expected_size=expected_size,
                    expected_digest=expected_digest,
                )
                if not _rename_noreplace(
                    source_name,
                    destination_name,
                    source_fd=directory_fd,
                    destination_fd=directory_fd,
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                _fsync_directory(directory_fd)
                self._delete_isolated_entry(
                    directory_fd,
                    destination_name,
                    expected,
                    expected_nlink=expected_nlink,
                    expected_size=expected_size,
                    expected_digest=expected_digest,
                )
            elif source is None and destination == expected:
                self._delete_isolated_entry(
                    directory_fd,
                    destination_name,
                    expected,
                    expected_nlink=expected_nlink,
                    expected_size=expected_size,
                    expected_digest=expected_digest,
                )
            elif source is not None or destination is not None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._retire_record(
                directory_fd,
                record_name=record_name,
                retired_name=retired_name,
                payload=payload,
                identity=record_identity,
            )
        leftovers = _bounded_names(directory_fd, _MAX_TREE_ENTRIES)
        if any(
            _DELETE_SLOT_NAME.fullmatch(name) is not None
            or _DELETE_INTENT_NAME.fullmatch(name) is not None
            or _DELETE_INTENT_TEMP_NAME.fullmatch(name) is not None
            for name in leftovers
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _recover_delete_tree(self, root_fd: int) -> None:
        visited = 0

        def visit(directory_fd: int) -> None:
            nonlocal visited
            self._recover_delete_transitions(directory_fd)
            remaining = _MAX_TREE_ENTRIES + 32 - visited
            for name in _bounded_names(directory_fd, remaining):
                visited += 1
                if visited > _MAX_TREE_ENTRIES + 32:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(entry.st_mode):
                    child, _ = _open_directory(directory_fd, name)
                    try:
                        visit(child)
                    finally:
                        os.close(child)

        visit(root_fd)

    def _delete_slot(
        self,
        directory_fd: int,
        name: str,
        *,
        expected: _RawEntry,
        expected_nlink: int,
        expected_digest: str | None,
        expected_size: int,
    ) -> tuple[str, str, str, bytes, _Identity]:
        status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            _raw_entry(directory_fd, name) != expected
            or _identity(status) != expected.identity
            or status.st_nlink != expected_nlink
            or status.st_size != expected_size
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        if stat.S_ISREG(status.st_mode):
            if expected_digest is None or expected_nlink != 1:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            target = None
        elif stat.S_ISLNK(status.st_mode):
            target = os.readlink(name, dir_fd=directory_fd)
        elif stat.S_ISDIR(status.st_mode):
            target = None
        else:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        identifier = uuid4().hex
        slot = f".delete-{identifier}"
        record_name = f"delete-intent-{identifier}.json"
        retired_name = f"retired-delete-intent-{identifier}.json"
        payload = self._delete_intent_payload(
            record_name=record_name,
            retired_name=retired_name,
            source_name=name,
            destination_name=slot,
            status=status,
            digest=expected_digest,
            target=target,
        )
        record_identity = self._publish_record(
            directory_fd,
            temporary_name=f".delete-intent-{identifier}.tmp",
            record_name=record_name,
            payload=payload,
        )
        if not _rename_noreplace(
            name,
            slot,
            source_fd=directory_fd,
            destination_fd=directory_fd,
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        _fsync_directory(directory_fd)
        self._checkpoint("cleanup:delete-renamed")
        return slot, record_name, retired_name, payload, record_identity

    def _unlink_identity(
        self,
        directory_fd: int,
        name: str,
        identity: _Identity,
        *,
        expected_digest: str | None = None,
        expected_size: int | None = None,
    ) -> None:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(current) != identity or (
            stat.S_ISREG(current.st_mode) and current.st_nlink != 1
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        expected = _raw_entry(directory_fd, name)
        payload: bytes | None = None
        if stat.S_ISREG(current.st_mode):
            if expected_digest is None or expected_size is None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            payload, opened_identity = _read_file(directory_fd, name, max_bytes=_MAX_FILE_BYTES)
            if (
                opened_identity != identity
                or expected_size is not None
                and len(payload) != expected_size
                or expected_digest is not None
                and hashlib.sha256(payload).hexdigest() != expected_digest
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        elif not stat.S_ISLNK(current.st_mode):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        assert expected is not None
        slot, record_name, retired_name, record_payload, record_identity = self._delete_slot(
            directory_fd,
            name,
            expected=expected,
            expected_nlink=current.st_nlink,
            expected_digest=expected_digest,
            expected_size=current.st_size,
        )
        self._delete_isolated_entry(
            directory_fd,
            slot,
            expected,
            expected_nlink=current.st_nlink,
            expected_size=current.st_size,
            expected_digest=expected_digest,
        )
        self._retire_record(
            directory_fd,
            record_name=record_name,
            retired_name=retired_name,
            payload=record_payload,
            identity=record_identity,
        )

    def _rmdir_identity(self, directory_fd: int, name: str, identity: _Identity) -> None:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(current) != identity or not stat.S_ISDIR(current.st_mode):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        expected = _raw_entry(directory_fd, name)
        if expected is None:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        slot, record_name, retired_name, record_payload, record_identity = self._delete_slot(
            directory_fd,
            name,
            expected=expected,
            expected_nlink=current.st_nlink,
            expected_digest=None,
            expected_size=current.st_size,
        )
        self._delete_isolated_entry(
            directory_fd,
            slot,
            expected,
            expected_nlink=current.st_nlink,
            expected_size=current.st_size,
            expected_digest=None,
        )
        self._retire_record(
            directory_fd,
            record_name=record_name,
            retired_name=retired_name,
            payload=record_payload,
            identity=record_identity,
        )

    def _remove_activation_marker(self, management: _Management, name: str) -> None:
        marker_name = f"{name}.json"
        marker_payload, identity = _read_file(
            management.activations, marker_name, max_bytes=_MAX_OWNER_BYTES
        )
        self._unlink_identity(
            management.activations,
            marker_name,
            identity,
            expected_digest=hashlib.sha256(marker_payload).hexdigest(),
            expected_size=len(marker_payload),
        )
        _fsync_directory(management.activations)

    def _activate(
        self,
        anchor: _RootAnchor,
        management: _Management,
        render: RenderedAgentSkill,
        target: str,
        active: _ActiveGeneration | None,
    ) -> None:
        old_target = active.target if active is not None else None
        state = self._read_state(
            management,
            book_key=render.manifest.book_key,
            skill_slug=render.skill_slug,
        )
        if (active is None and state is not None) or (
            active is not None and (state is None or state[0].current_target != active.target)
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        activation, new_identity = self._create_activation(management, render, target, old_target)
        self._checkpoint("activation:before")
        self._checkpoint("hierarchy:before-cas")
        management.verify(anchor)
        self._checkpoint("activation:precondition")
        if active is None:
            if self._active_pointer(anchor, render.skill_slug) is not None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            moved = _rename_noreplace(
                activation,
                render.skill_slug,
                source_fd=management.activations,
                destination_fd=anchor.descriptor,
            )
            if not moved:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            _fsync_directory(anchor.descriptor)
            current = self._active_pointer(anchor, render.skill_slug)
            if current != (target, new_identity):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        else:
            current = self._active_pointer(anchor, render.skill_slug)
            if current != (active.target, active.pointer_identity):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._checkpoint("activation:exchange")
            expected_new = _RawEntry(stat.S_IFLNK, new_identity, target)
            expected_old = _RawEntry(stat.S_IFLNK, active.pointer_identity, active.target)
            try:
                _rename_exchange(
                    activation,
                    render.skill_slug,
                    source_fd=management.activations,
                    destination_fd=anchor.descriptor,
                )
                _fsync_directory(anchor.descriptor)
                _fsync_directory(management.activations)
                self._checkpoint("activation:exchanged")
                if (
                    _raw_entry(anchor.descriptor, render.skill_slug) != expected_new
                    or _raw_entry(management.activations, activation) != expected_old
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            except BaseException:
                if _raw_entry(anchor.descriptor, render.skill_slug) == expected_new:
                    self._rollback_exchange(
                        anchor,
                        management,
                        render.skill_slug,
                        activation,
                        expected_new,
                    )
                raise
        self._checkpoint("activation:after")
        self._write_state(
            management,
            book_key=render.manifest.book_key,
            skill_slug=render.skill_slug,
            current_target=target,
            previous_target=old_target,
            scratch_name=activation,
        )
        self._checkpoint("cleanup:start")
        try:
            leftover = os.stat(activation, dir_fd=management.activations, follow_symlinks=False)
        except FileNotFoundError:
            leftover = None
        if leftover is not None:
            expected_target = active.target if active is not None else target
            if (
                not stat.S_ISLNK(leftover.st_mode)
                or os.readlink(activation, dir_fd=management.activations) != expected_target
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._unlink_identity(management.activations, activation, _identity(leftover))
        self._remove_activation_marker(management, activation)

    def _rollback_exchange(
        self,
        anchor: _RootAnchor,
        management: _Management,
        active_name: str,
        activation: str,
        expected_new: _RawEntry,
    ) -> None:
        displaced = _raw_entry(management.activations, activation)
        if _raw_entry(anchor.descriptor, active_name) != expected_new or displaced is None:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        _rename_exchange(
            activation,
            active_name,
            source_fd=management.activations,
            destination_fd=anchor.descriptor,
        )
        _fsync_directory(management.activations)
        _fsync_directory(anchor.descriptor)
        if (
            _raw_entry(anchor.descriptor, active_name) != displaced
            or _raw_entry(management.activations, activation) != expected_new
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _active_pointer_in(
        self, directory_fd: int, name: str, *, validate_target: bool
    ) -> tuple[str, _Identity] | None:
        try:
            entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISLNK(entry.st_mode):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        target = os.readlink(name, dir_fd=directory_fd)
        if validate_target:
            self._target_parts(target)
        return target, _identity(entry)

    def _recover_activations(self, anchor: _RootAnchor, management: _Management) -> None:
        names = _bounded_names(management.activations, _MAX_ACTIVATION_ENTRIES)
        marker_names = sorted(name for name in names if name.endswith(".json"))
        for marker_name in marker_names:
            activation = marker_name.removesuffix(".json")
            if _ACTIVATION_NAME.fullmatch(activation) is None:
                continue
            marker_bytes, marker_identity = _read_file(
                management.activations, marker_name, max_bytes=_MAX_OWNER_BYTES
            )
            marker = _load_unique_json(marker_bytes)
            expected_keys = {
                "active_name",
                "book_key",
                "name",
                "new_target",
                "old_target",
                "owner",
                "schema",
            }
            if set(marker) != expected_keys or marker.get("name") != activation:
                continue
            active_name = marker.get("active_name")
            book_key = marker.get("book_key")
            new_target = marker.get("new_target")
            old_target = marker.get("old_target")
            if (
                marker.get("owner") != "cove-book-forge-skill-activation"
                or marker.get("schema") != 1
                or not isinstance(active_name, str)
                or not isinstance(book_key, str)
                or not isinstance(new_target, str)
                or old_target is not None
                and not isinstance(old_target, str)
            ):
                continue
            new_manifest, _ = self._validate_target(
                management, new_target, expected_slug=active_name, anchor=anchor
            )
            if new_manifest.book_key != book_key:
                continue
            if isinstance(old_target, str):
                old_manifest, _ = self._validate_target(
                    management, old_target, expected_slug=active_name, anchor=anchor
                )
                if old_manifest.book_key != book_key:
                    continue
            root_pointer = self._active_pointer(anchor, active_name)
            staged_pointer = self._active_pointer_in(
                management.activations, activation, validate_target=True
            )
            allowed = {
                (old_target, new_target),
                (new_target, old_target),
                (new_target, None),
                (old_target, None),
            }
            state = (
                root_pointer[0] if root_pointer is not None else None,
                staged_pointer[0] if staged_pointer is not None else None,
            )
            if state not in allowed:
                continue
            durable = self._read_state(management, book_key=book_key, skill_slug=active_name)
            if state[0] == new_target:
                self._write_state(
                    management,
                    book_key=book_key,
                    skill_slug=active_name,
                    current_target=new_target,
                    previous_target=old_target,
                    scratch_name=activation,
                )
            elif old_target is None:
                if durable is not None:
                    continue
            elif durable is None or durable[0].current_target != old_target:
                continue
            if staged_pointer is not None:
                self._unlink_identity(management.activations, activation, staged_pointer[1])
            self._unlink_identity(
                management.activations,
                marker_name,
                marker_identity,
                expected_digest=hashlib.sha256(marker_bytes).hexdigest(),
                expected_size=len(marker_bytes),
            )
            _fsync_directory(management.activations)

    def _recover_transactions(self, management: _Management) -> None:
        names = _bounded_names(management.transactions, _MAX_TRANSACTION_COUNT)
        for name in names:
            if _TX_NAME.fullmatch(name) is not None:
                self._remove_owned_transaction(management, name, require_owned=False)

    def _remove_owned_transaction(
        self, management: _Management, name: str, *, require_owned: bool = True
    ) -> None:
        match = _TX_NAME.fullmatch(name)
        if match is None:
            if require_owned:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            return
        try:
            tx_fd, tx_identity = _open_directory(management.transactions, name)
        except (FileNotFoundError, ForgeException):
            if require_owned:
                raise
            return
        try:
            self._recover_delete_tree(tx_fd)
            names = set(_bounded_names(tx_fd, 3))
            if _OWNER not in names or not names <= {_OWNER, _CONTENT}:
                if require_owned:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                return
            owner_bytes, owner_identity = _read_file(tx_fd, _OWNER, max_bytes=_MAX_OWNER_BYTES)
            owner = _load_unique_json(owner_bytes)
            expected_files = owner.get("files")
            if (
                owner.get("owner") != "cove-book-forge-skill-transaction"
                or owner.get("schema") != 1
                or owner.get("tx_id") != match.group(1)
                or not isinstance(expected_files, dict)
                or not all(
                    isinstance(path, str)
                    and isinstance(digest, str)
                    and re.fullmatch(r"[0-9a-f]{64}", digest)
                    for path, digest in expected_files.items()
                )
            ):
                if require_owned:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                return
            if _CONTENT in names:
                content_fd, content_identity = _open_directory(tx_fd, _CONTENT)
                try:
                    files, _ = self._read_tree(content_fd)
                    if not set(files) <= set(expected_files) or any(
                        hashlib.sha256(payload).hexdigest() != expected_files[path]
                        for path, payload in files.items()
                    ):
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                finally:
                    os.close(content_fd)
            audit = self._audit_tree(tx_fd)
            allowed_directories = {_CONTENT}
            allowed_files = {_OWNER}
            expected_digests = {_OWNER: hashlib.sha256(owner_bytes).hexdigest()}
            for path in expected_files:
                managed_path = f"{_CONTENT}/{path}"
                allowed_files.add(managed_path)
                expected_digests[managed_path] = expected_files[path]
                components = managed_path.split("/")[:-1]
                allowed_directories.update(
                    "/".join(components[: index + 1]) for index in range(len(components))
                )
            if any(
                path not in allowed_directories | allowed_files
                or item.directory != (path in allowed_directories)
                or not item.directory
                and item.digest != expected_digests[path]
                for path, item in audit.items()
            ):
                if require_owned:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                return
            if (
                audit.get(_OWNER) is None
                or audit[_OWNER].directory
                or audit[_OWNER].identity != owner_identity
                or _CONTENT in names
                and (
                    audit.get(_CONTENT) is None
                    or not audit[_CONTENT].directory
                    or audit[_CONTENT].identity != content_identity
                )
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._remove_audited_payload(tx_fd, audit)
        finally:
            os.close(tx_fd)
        self._rmdir_identity(management.transactions, name, tx_identity)

    def _audit_tree(self, directory_fd: int) -> dict[str, _AuditEntry]:
        audit: dict[str, _AuditEntry] = {}
        total_bytes = 0

        def visit(current_fd: int, prefix: str) -> None:
            nonlocal total_bytes
            remaining_entries = _MAX_TREE_ENTRIES - len(audit)
            for name in _bounded_names(current_fd, remaining_entries):
                if len(audit) >= _MAX_TREE_ENTRIES:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                path = f"{prefix}/{name}" if prefix else name
                try:
                    validate_relative_path(path)
                except ValueError:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
                before = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                if stat.S_ISDIR(before.st_mode):
                    child, opened_identity = _open_directory(current_fd, name)
                    try:
                        if opened_identity != _identity(before):
                            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                        audit[path] = _AuditEntry(True, opened_identity)
                        visit(child, path)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(before.st_mode):
                    if before.st_nlink != 1:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    remaining_bytes = _MAX_TOTAL_TREE_BYTES - total_bytes
                    payload, identity = _read_file(
                        current_fd,
                        name,
                        max_bytes=min(_MAX_FILE_BYTES, remaining_bytes),
                    )
                    after = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                    if (
                        _identity(after) != identity
                        or after.st_nlink != 1
                        or after.st_size != len(payload)
                    ):
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    total_bytes += len(payload)
                    audit[path] = _AuditEntry(
                        False,
                        identity,
                        1,
                        len(payload),
                        hashlib.sha256(payload).hexdigest(),
                    )
                else:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

        visit(directory_fd, "")
        return audit

    def _audit_payload(
        self,
        payload_fd: int,
        expected: Mapping[str, _AuditEntry],
        *,
        allow_missing: bool,
    ) -> None:
        current = self._audit_tree(payload_fd)
        if (not allow_missing and set(current) != set(expected)) or not set(current) <= set(
            expected
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        if any(current[path] != expected[path] for path in current):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _journal_payload(
        self,
        *,
        quarantine_name: str,
        generation_name: str,
        generation_identity: _Identity,
        manifest: AgentSkillManifest,
        audit: Mapping[str, _AuditEntry],
    ) -> bytes:
        entries: dict[str, object] = {}
        for path, item in sorted(audit.items()):
            entries[path] = {
                "dev": item.identity[0],
                "digest": item.digest,
                "directory": item.directory,
                "ino": item.identity[1],
                "nlink": item.nlink,
                "size": item.size,
            }
        payload = _canonical_json(
            {
                "book_key": manifest.book_key,
                "entries": entries,
                "generation_dev": generation_identity[0],
                "generation_ino": generation_identity[1],
                "generation_name": generation_name,
                "manifest_checksum": manifest.checksum,
                "owner": "cove-book-forge-generation-quarantine",
                "quarantine_name": quarantine_name,
                "schema": 1,
                "skill_slug": manifest.skill_slug,
            }
        )
        if len(payload) > _MAX_OWNER_BYTES:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        return payload

    def _parse_journal(
        self, payload: bytes, *, quarantine_name: str
    ) -> tuple[_Identity, dict[str, _AuditEntry]]:
        journal = _load_unique_json(payload)
        if (
            set(journal)
            != {
                "book_key",
                "entries",
                "generation_dev",
                "generation_ino",
                "generation_name",
                "manifest_checksum",
                "owner",
                "quarantine_name",
                "schema",
                "skill_slug",
            }
            or journal.get("owner") != "cove-book-forge-generation-quarantine"
            or journal.get("schema") != 1
            or journal.get("quarantine_name") != quarantine_name
            or not isinstance(journal.get("book_key"), str)
            or re.fullmatch(r"[0-9a-f]{16}", journal["book_key"]) is None
            or not isinstance(journal.get("generation_name"), str)
            or _GENERATION_NAME.fullmatch(journal["generation_name"]) is None
            or not isinstance(journal.get("manifest_checksum"), str)
            or f"gen-{journal['manifest_checksum']}" != journal["generation_name"]
            or not isinstance(journal.get("skill_slug"), str)
            or not isinstance(journal.get("generation_dev"), int)
            or not isinstance(journal.get("generation_ino"), int)
            or not isinstance(journal.get("entries"), dict)
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        audit: dict[str, _AuditEntry] = {}
        total_bytes = 0
        for path, raw in journal["entries"].items():
            if not isinstance(path, str) or not isinstance(raw, dict):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            try:
                validate_relative_path(path)
            except ValueError:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
            if set(raw) != {"dev", "digest", "directory", "ino", "nlink", "size"}:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            directory = raw.get("directory")
            dev = raw.get("dev")
            ino = raw.get("ino")
            nlink = raw.get("nlink")
            size = raw.get("size")
            digest = raw.get("digest")
            if (
                not isinstance(directory, bool)
                or not isinstance(dev, int)
                or not isinstance(ino, int)
                or directory
                and (nlink is not None or size is not None or digest is not None)
                or not directory
                and (
                    nlink != 1
                    or not isinstance(size, int)
                    or size < 0
                    or size > _MAX_FILE_BYTES
                    or not isinstance(digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                )
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            audit[path] = _AuditEntry(directory, (dev, ino), nlink, size, digest)
            if not directory:
                assert isinstance(size, int)
                total_bytes += size
        if len(audit) > _MAX_TREE_ENTRIES or total_bytes > _MAX_TOTAL_TREE_BYTES:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        return (journal["generation_dev"], journal["generation_ino"]), audit

    def _stage_record_payload(
        self,
        *,
        identifier: str,
        journal_payload: bytes,
        stage_identity: _Identity | None,
        intent_checksum: str | None,
    ) -> bytes:
        record_kind = "stage-intent" if stage_identity is None else "stage-ready"
        split = max(1, len(journal_payload) // 2)
        return _checksummed_record(
            {
                "intent_checksum": intent_checksum,
                "journal_partial_sha256": hashlib.sha256(journal_payload[:split]).hexdigest(),
                "journal_partial_size": split,
                "journal_sha256": hashlib.sha256(journal_payload).hexdigest(),
                "journal_size": len(journal_payload),
                "owner": f"cove-book-forge-{record_kind}",
                "quarantine_name": f"q-{identifier}",
                "record_name": f"{record_kind}-{identifier}.json",
                "retired_name": f"retired-{record_kind}-{identifier}.json",
                "schema": 1,
                "stage_dev": stage_identity[0] if stage_identity is not None else None,
                "stage_ino": stage_identity[1] if stage_identity is not None else None,
                "stage_name": f"stage-{identifier}",
            }
        )

    def _parse_stage_record(
        self,
        payload: bytes,
        *,
        record_name: str,
        ready: bool,
    ) -> dict[str, Any]:
        record = _load_checksummed_record(payload)
        pattern = _STAGE_READY_NAME if ready else _STAGE_INTENT_NAME
        match = pattern.fullmatch(record_name)
        identifier = match.group(1) if match is not None else ""
        record_kind = "stage-ready" if ready else "stage-intent"
        if (
            match is None
            or set(record)
            != {
                "checksum",
                "intent_checksum",
                "journal_partial_sha256",
                "journal_partial_size",
                "journal_sha256",
                "journal_size",
                "owner",
                "quarantine_name",
                "record_name",
                "retired_name",
                "schema",
                "stage_dev",
                "stage_ino",
                "stage_name",
            }
            or record.get("owner") != f"cove-book-forge-{record_kind}"
            or record.get("schema") != 1
            or record.get("record_name") != record_name
            or record.get("retired_name") != f"retired-{record_kind}-{identifier}.json"
            or record.get("stage_name") != f"stage-{identifier}"
            or record.get("quarantine_name") != f"q-{identifier}"
            or not isinstance(record.get("journal_size"), int)
            or not 0 < record["journal_size"] <= _MAX_OWNER_BYTES
            or not isinstance(record.get("journal_partial_size"), int)
            or not 0 < record["journal_partial_size"] <= record["journal_size"]
            or not isinstance(record.get("journal_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record["journal_sha256"]) is None
            or not isinstance(record.get("journal_partial_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record["journal_partial_sha256"]) is None
            or ready
            and (
                not isinstance(record.get("stage_dev"), int)
                or not isinstance(record.get("stage_ino"), int)
                or not isinstance(record.get("intent_checksum"), str)
                or re.fullmatch(r"[0-9a-f]{64}", record["intent_checksum"]) is None
            )
            or not ready
            and (
                record.get("stage_dev") is not None
                or record.get("stage_ino") is not None
                or record.get("intent_checksum") is not None
            )
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        return record

    def _closing_intent_payload(
        self,
        *,
        quarantine_name: str,
        wrapper_identity: _Identity,
        closing_payload: bytes,
    ) -> bytes:
        split = max(1, len(closing_payload) // 2)
        return _checksummed_record(
            {
                "closing_partial_sha256": hashlib.sha256(closing_payload[:split]).hexdigest(),
                "closing_partial_size": split,
                "closing_sha256": hashlib.sha256(closing_payload).hexdigest(),
                "closing_size": len(closing_payload),
                "final_name": f"closing-{quarantine_name}.json",
                "owner": "cove-book-forge-closing-intent",
                "partial_name": f".closing-{quarantine_name}.partial",
                "quarantine_name": quarantine_name,
                "record_name": f"closing-intent-{quarantine_name}.json",
                "retired_name": f"retired-closing-intent-{quarantine_name}.json",
                "schema": 1,
                "wrapper_dev": wrapper_identity[0],
                "wrapper_ino": wrapper_identity[1],
            }
        )

    def _parse_closing_intent(self, payload: bytes, *, record_name: str) -> dict[str, Any]:
        record = _load_checksummed_record(payload)
        match = _CLOSING_INTENT_NAME.fullmatch(record_name)
        quarantine_name = match.group(1) if match is not None else ""
        if (
            match is None
            or set(record)
            != {
                "checksum",
                "closing_partial_sha256",
                "closing_partial_size",
                "closing_sha256",
                "closing_size",
                "final_name",
                "owner",
                "partial_name",
                "quarantine_name",
                "record_name",
                "retired_name",
                "schema",
                "wrapper_dev",
                "wrapper_ino",
            }
            or record.get("owner") != "cove-book-forge-closing-intent"
            or record.get("schema") != 1
            or record.get("record_name") != record_name
            or record.get("retired_name") != f"retired-closing-intent-{quarantine_name}.json"
            or record.get("quarantine_name") != quarantine_name
            or record.get("final_name") != f"closing-{quarantine_name}.json"
            or record.get("partial_name") != f".closing-{quarantine_name}.partial"
            or not isinstance(record.get("wrapper_dev"), int)
            or not isinstance(record.get("wrapper_ino"), int)
            or not isinstance(record.get("closing_size"), int)
            or not 0 < record["closing_size"] <= _MAX_OWNER_BYTES
            or not isinstance(record.get("closing_partial_size"), int)
            or not 0 < record["closing_partial_size"] <= record["closing_size"]
            or not isinstance(record.get("closing_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record["closing_sha256"]) is None
            or not isinstance(record.get("closing_partial_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record["closing_partial_sha256"]) is None
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        return record

    def _stage_records_match(
        self,
        intent: Mapping[str, Any],
        ready: Mapping[str, Any],
    ) -> bool:
        return ready["intent_checksum"] == intent["checksum"] and all(
            ready[key] == intent[key]
            for key in (
                "journal_partial_sha256",
                "journal_partial_size",
                "journal_sha256",
                "journal_size",
                "quarantine_name",
                "stage_name",
            )
        )

    def _recover_quarantine_record_temporaries(self, management: _Management) -> None:
        names = _bounded_names(management.quarantine, _MAX_QUARANTINE_ENTRIES)
        for temporary_name in sorted(names):
            stage_intent_match = _STAGE_INTENT_TEMP_NAME.fullmatch(temporary_name)
            stage_ready_match = _STAGE_READY_TEMP_NAME.fullmatch(temporary_name)
            closing_match = _CLOSING_INTENT_TEMP_NAME.fullmatch(temporary_name)
            if stage_intent_match is not None:
                identifier = stage_intent_match.group(1)
                record_name = f"stage-intent-{identifier}.json"
                payload, temporary_identity = _read_file(
                    management.quarantine,
                    temporary_name,
                    max_bytes=_MAX_OWNER_BYTES,
                )
                intent = self._parse_stage_record(
                    payload,
                    record_name=record_name,
                    ready=False,
                )
                related_names = (
                    record_name,
                    intent["stage_name"],
                    intent["quarantine_name"],
                    f"stage-ready-{identifier}.json",
                    f".stage-ready-{identifier}.tmp",
                )
                if any(
                    _raw_entry(management.quarantine, name) is not None for name in related_names
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            elif stage_ready_match is not None:
                identifier = stage_ready_match.group(1)
                record_name = f"stage-ready-{identifier}.json"
                payload, temporary_identity = _read_file(
                    management.quarantine,
                    temporary_name,
                    max_bytes=_MAX_OWNER_BYTES,
                )
                ready = self._parse_stage_record(
                    payload,
                    record_name=record_name,
                    ready=True,
                )
                intent_name = f"stage-intent-{identifier}.json"
                intent_payload, _ = _read_file(
                    management.quarantine,
                    intent_name,
                    max_bytes=_MAX_OWNER_BYTES,
                )
                intent = self._parse_stage_record(
                    intent_payload,
                    record_name=intent_name,
                    ready=False,
                )
                stage = _raw_entry(management.quarantine, ready["stage_name"])
                if (
                    not self._stage_records_match(intent, ready)
                    or _raw_entry(management.quarantine, record_name) is not None
                    or _raw_entry(management.quarantine, ready["quarantine_name"]) is not None
                    or stage
                    != _RawEntry(
                        stat.S_IFDIR,
                        (ready["stage_dev"], ready["stage_ino"]),
                        None,
                    )
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                stage_fd, stage_identity = _open_directory(
                    management.quarantine,
                    ready["stage_name"],
                )
                try:
                    if stage_identity != stage.identity or _bounded_names(stage_fd, 1):
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                finally:
                    os.close(stage_fd)
            elif closing_match is not None:
                quarantine_name = closing_match.group(1)
                record_name = f"closing-intent-{quarantine_name}.json"
                payload, temporary_identity = _read_file(
                    management.quarantine,
                    temporary_name,
                    max_bytes=_MAX_OWNER_BYTES,
                )
                intent = self._parse_closing_intent(payload, record_name=record_name)
                wrapper = _raw_entry(management.quarantine, quarantine_name)
                expected_wrapper = _RawEntry(
                    stat.S_IFDIR,
                    (intent["wrapper_dev"], intent["wrapper_ino"]),
                    None,
                )
                if (
                    _raw_entry(management.quarantine, record_name) is not None
                    or _raw_entry(management.quarantine, intent["partial_name"]) is not None
                    or _raw_entry(management.quarantine, intent["final_name"]) is not None
                    or wrapper != expected_wrapper
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                wrapper_fd, wrapper_identity = _open_directory(
                    management.quarantine,
                    quarantine_name,
                )
                try:
                    if wrapper_identity != expected_wrapper.identity or set(
                        _bounded_names(wrapper_fd, 3)
                    ) != {"journal.json", "verified.json"}:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    journal_payload, _ = _read_file(
                        wrapper_fd,
                        "journal.json",
                        max_bytes=_MAX_OWNER_BYTES,
                    )
                    verified_payload, _ = _read_file(
                        wrapper_fd,
                        "verified.json",
                        max_bytes=_MAX_OWNER_BYTES,
                    )
                finally:
                    os.close(wrapper_fd)
                closing_payload = self._closing_payload(
                    quarantine_name=quarantine_name,
                    wrapper_identity=wrapper_identity,
                    journal_payload=journal_payload,
                    verified_payload=verified_payload,
                )
                if (
                    len(closing_payload) != intent["closing_size"]
                    or hashlib.sha256(closing_payload).hexdigest() != intent["closing_sha256"]
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            else:
                continue
            self._finish_record_publication(
                management.quarantine,
                temporary_name=temporary_name,
                record_name=record_name,
                payload=payload,
                temporary_identity=temporary_identity,
            )

    def _verified_payload(self, journal_payload: bytes) -> bytes:
        return _canonical_json(
            {
                "journal_sha256": hashlib.sha256(journal_payload).hexdigest(),
                "owner": "cove-book-forge-verified-quarantine",
                "schema": 1,
            }
        )

    def _closing_payload(
        self,
        *,
        quarantine_name: str,
        wrapper_identity: _Identity,
        journal_payload: bytes,
        verified_payload: bytes,
    ) -> bytes:
        return _canonical_json(
            {
                "journal_sha256": hashlib.sha256(journal_payload).hexdigest(),
                "owner": "cove-book-forge-closing-quarantine",
                "quarantine_name": quarantine_name,
                "schema": 1,
                "verified_sha256": hashlib.sha256(verified_payload).hexdigest(),
                "wrapper_dev": wrapper_identity[0],
                "wrapper_ino": wrapper_identity[1],
            }
        )

    def _parse_closing(
        self, payload: bytes, *, closing_name: str
    ) -> tuple[str, _Identity, Mapping[str, str]]:
        closing = _load_unique_json(payload)
        match = _QUARANTINE_CLOSING_NAME.fullmatch(closing_name)
        if (
            match is None
            or set(closing)
            != {
                "journal_sha256",
                "owner",
                "quarantine_name",
                "schema",
                "verified_sha256",
                "wrapper_dev",
                "wrapper_ino",
            }
            or closing.get("owner") != "cove-book-forge-closing-quarantine"
            or closing.get("schema") != 1
            or closing.get("quarantine_name") != match.group(1)
            or not isinstance(closing.get("wrapper_dev"), int)
            or not isinstance(closing.get("wrapper_ino"), int)
            or not isinstance(closing.get("journal_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", closing["journal_sha256"]) is None
            or not isinstance(closing.get("verified_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", closing["verified_sha256"]) is None
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        digests = {
            "journal.json": closing["journal_sha256"],
            "verified.json": closing["verified_sha256"],
        }
        return (
            closing["quarantine_name"],
            (closing["wrapper_dev"], closing["wrapper_ino"]),
            digests,
        )

    def _finish_closing(
        self,
        management: _Management,
        *,
        closing_name: str,
        closing_payload: bytes,
        closing_identity: _Identity,
    ) -> None:
        quarantine_name, wrapper_identity, digests = self._parse_closing(
            closing_payload, closing_name=closing_name
        )
        raw_wrapper = _raw_entry(management.quarantine, quarantine_name)
        if raw_wrapper is not None:
            if raw_wrapper.mode_type != stat.S_IFDIR or raw_wrapper.identity != wrapper_identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            wrapper_fd, opened_identity = _open_directory(management.quarantine, quarantine_name)
            try:
                if opened_identity != wrapper_identity:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                names = set(_bounded_names(wrapper_fd, 3))
                if not names <= set(digests):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                for name in sorted(names):
                    payload, identity = _read_file(wrapper_fd, name, max_bytes=_MAX_OWNER_BYTES)
                    if hashlib.sha256(payload).hexdigest() != digests[name]:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    self._unlink_identity(
                        wrapper_fd,
                        name,
                        identity,
                        expected_digest=digests[name],
                        expected_size=len(payload),
                    )
            finally:
                os.close(wrapper_fd)
            self._checkpoint("cleanup:terminal-journal-removed")
            self._rmdir_identity(management.quarantine, quarantine_name, wrapper_identity)
        stored, stored_identity = _read_file(
            management.quarantine, closing_name, max_bytes=_MAX_OWNER_BYTES
        )
        if stored != closing_payload or stored_identity != closing_identity:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        self._unlink_identity(
            management.quarantine,
            closing_name,
            closing_identity,
            expected_digest=hashlib.sha256(closing_payload).hexdigest(),
            expected_size=len(closing_payload),
        )

    def _close_wrapper(
        self,
        management: _Management,
        *,
        quarantine_name: str,
        wrapper_fd: int,
        journal_payload: bytes,
        verified_payload: bytes,
    ) -> None:
        wrapper_identity = _identity(os.fstat(wrapper_fd))
        closing_name = f"closing-{quarantine_name}.json"
        closing_payload = self._closing_payload(
            quarantine_name=quarantine_name,
            wrapper_identity=wrapper_identity,
            journal_payload=journal_payload,
            verified_payload=verified_payload,
        )
        intent_name = f"closing-intent-{quarantine_name}.json"
        retired_name = f"retired-closing-intent-{quarantine_name}.json"
        intent_payload = self._closing_intent_payload(
            quarantine_name=quarantine_name,
            wrapper_identity=wrapper_identity,
            closing_payload=closing_payload,
        )
        intent_identity = self._publish_record(
            management.quarantine,
            temporary_name=f".closing-intent-{quarantine_name}.tmp",
            record_name=intent_name,
            payload=intent_payload,
        )
        closing_identity = self._write_interrupted_file(
            management.quarantine,
            partial_name=f".closing-{quarantine_name}.partial",
            final_name=closing_name,
            payload=closing_payload,
            checkpoint="cleanup:closing-writing",
        )
        self._retire_record(
            management.quarantine,
            record_name=intent_name,
            retired_name=retired_name,
            payload=intent_payload,
            identity=intent_identity,
        )
        self._finish_closing(
            management,
            closing_name=closing_name,
            closing_payload=closing_payload,
            closing_identity=closing_identity,
        )

    def _recover_closing_intents(self, management: _Management, names: tuple[str, ...]) -> None:
        for intent_name in sorted(names):
            if _CLOSING_INTENT_NAME.fullmatch(intent_name) is None:
                continue
            intent_payload, intent_identity = _read_file(
                management.quarantine, intent_name, max_bytes=_MAX_OWNER_BYTES
            )
            intent = self._parse_closing_intent(intent_payload, record_name=intent_name)
            quarantine_name = intent["quarantine_name"]
            expected_wrapper = _RawEntry(
                stat.S_IFDIR,
                (intent["wrapper_dev"], intent["wrapper_ino"]),
                None,
            )
            wrapper = _raw_entry(management.quarantine, quarantine_name)
            if wrapper is not None and wrapper != expected_wrapper:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            partial_name = intent["partial_name"]
            final_name = intent["final_name"]
            partial = _raw_entry(management.quarantine, partial_name)
            final = _raw_entry(management.quarantine, final_name)
            if partial is not None and final is not None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if partial is not None:
                payload, identity = _read_file(
                    management.quarantine,
                    partial_name,
                    max_bytes=intent["closing_size"],
                )
                digest = hashlib.sha256(payload).hexdigest()
                allowed = {
                    (intent["closing_partial_size"], intent["closing_partial_sha256"]),
                    (intent["closing_size"], intent["closing_sha256"]),
                }
                if identity != partial.identity or (len(payload), digest) not in allowed:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                self._unlink_identity(
                    management.quarantine,
                    partial_name,
                    identity,
                    expected_digest=digest,
                    expected_size=len(payload),
                )
            if final is not None:
                payload, identity = _read_file(
                    management.quarantine,
                    final_name,
                    max_bytes=intent["closing_size"],
                )
                if (
                    identity != final.identity
                    or len(payload) != intent["closing_size"]
                    or hashlib.sha256(payload).hexdigest() != intent["closing_sha256"]
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                self._parse_closing(payload, closing_name=final_name)
            self._retire_record(
                management.quarantine,
                record_name=intent_name,
                retired_name=intent["retired_name"],
                payload=intent_payload,
                identity=intent_identity,
            )

    def _remove_audited_payload(
        self,
        directory_fd: int,
        expected: Mapping[str, _AuditEntry],
        *,
        prefix: str = "",
        checkpointed: list[bool] | None = None,
    ) -> None:
        if checkpointed is None:
            checkpointed = [False]
        names = _bounded_names(directory_fd, _MAX_TREE_ENTRIES)
        for name in sorted(names):
            path = f"{prefix}/{name}" if prefix else name
            item = expected.get(path)
            if item is None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if _identity(current) != item.identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if item.directory:
                if not stat.S_ISDIR(current.st_mode):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                child, opened_identity = _open_directory(directory_fd, name)
                try:
                    if opened_identity != item.identity:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    self._remove_audited_payload(
                        child,
                        expected,
                        prefix=path,
                        checkpointed=checkpointed,
                    )
                finally:
                    os.close(child)
                self._rmdir_identity(directory_fd, name, item.identity)
            else:
                if item.size is None or item.digest is None:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                payload, identity = _read_file(directory_fd, name, max_bytes=item.size)
                after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    identity != item.identity
                    or _identity(after) != item.identity
                    or after.st_nlink != 1
                    or after.st_size != item.size
                    or hashlib.sha256(payload).hexdigest() != item.digest
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                self._unlink_identity(
                    directory_fd,
                    name,
                    item.identity,
                    expected_digest=item.digest,
                    expected_size=item.size,
                )
            _fsync_directory(directory_fd)
            if not checkpointed[0]:
                checkpointed[0] = True
                self._checkpoint("cleanup:partial")

    def _finish_quarantine(
        self,
        management: _Management,
        quarantine_name: str,
        wrapper_fd: int,
        generation_identity: _Identity,
        audit: Mapping[str, _AuditEntry],
        journal_payload: bytes,
    ) -> None:
        verified_payload = self._verified_payload(journal_payload)
        try:
            stored_verified, _ = _read_file(wrapper_fd, "verified.json", max_bytes=_MAX_OWNER_BYTES)
        except FileNotFoundError:
            payload_fd, payload_identity = _open_directory(wrapper_fd, "payload")
            try:
                if payload_identity != generation_identity:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                self._audit_payload(payload_fd, audit, allow_missing=False)
            finally:
                os.close(payload_fd)
            _write_file(wrapper_fd, "verified.json", verified_payload)
            _fsync_directory(wrapper_fd)
        else:
            if stored_verified != verified_payload:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

        self._checkpoint("cleanup:quarantined")
        remaining_fd: int | None
        try:
            remaining_fd, payload_identity = _open_directory(wrapper_fd, "payload")
        except FileNotFoundError:
            remaining_fd = None
        if remaining_fd is not None:
            try:
                if payload_identity != generation_identity:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                self._audit_payload(remaining_fd, audit, allow_missing=True)
                self._remove_audited_payload(remaining_fd, audit)
            finally:
                os.close(remaining_fd)
            self._rmdir_identity(wrapper_fd, "payload", generation_identity)

        self._close_wrapper(
            management,
            quarantine_name=quarantine_name,
            wrapper_fd=wrapper_fd,
            journal_payload=journal_payload,
            verified_payload=verified_payload,
        )

    def _recover_closings(self, management: _Management, names: tuple[str, ...]) -> None:
        for closing_name in sorted(names):
            if _QUARANTINE_CLOSING_NAME.fullmatch(closing_name) is None:
                continue
            closing_payload, closing_identity = _read_file(
                management.quarantine, closing_name, max_bytes=_MAX_OWNER_BYTES
            )
            self._finish_closing(
                management,
                closing_name=closing_name,
                closing_payload=closing_payload,
                closing_identity=closing_identity,
            )

    def _recover_stage_intents(self, management: _Management, names: tuple[str, ...]) -> None:
        recovered_ready: set[str] = set()
        for intent_name in sorted(names):
            match = _STAGE_INTENT_NAME.fullmatch(intent_name)
            if match is None:
                continue
            identifier = match.group(1)
            intent_payload, intent_identity = _read_file(
                management.quarantine, intent_name, max_bytes=_MAX_OWNER_BYTES
            )
            intent = self._parse_stage_record(intent_payload, record_name=intent_name, ready=False)
            ready_name = f"stage-ready-{identifier}.json"
            ready_payload: bytes | None = None
            ready_identity: _Identity | None = None
            ready: dict[str, Any] | None = None
            if _raw_entry(management.quarantine, ready_name) is not None:
                ready_payload, ready_identity = _read_file(
                    management.quarantine, ready_name, max_bytes=_MAX_OWNER_BYTES
                )
                ready = self._parse_stage_record(ready_payload, record_name=ready_name, ready=True)
                if not self._stage_records_match(intent, ready):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                recovered_ready.add(ready_name)
            stage_name = intent["stage_name"]
            quarantine_name = intent["quarantine_name"]
            stage = _raw_entry(management.quarantine, stage_name)
            quarantine = _raw_entry(management.quarantine, quarantine_name)
            if stage is not None and quarantine is not None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if stage is not None:
                if (
                    stage.mode_type != stat.S_IFDIR
                    or ready is not None
                    and stage.identity
                    != (
                        ready["stage_dev"],
                        ready["stage_ino"],
                    )
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                wrapper_fd, wrapper_identity = _open_directory(management.quarantine, stage_name)
                try:
                    self._recover_delete_tree(wrapper_fd)
                    wrapper_names = set(_bounded_names(wrapper_fd, 3))
                    if not wrapper_names <= {"journal.json", "journal.partial"} or (
                        ready is None and wrapper_names
                    ):
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    if len(wrapper_names) > 1:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    for journal_name in wrapper_names:
                        payload, identity = _read_file(
                            wrapper_fd,
                            journal_name,
                            max_bytes=intent["journal_size"],
                        )
                        digest = hashlib.sha256(payload).hexdigest()
                        if journal_name == "journal.json":
                            allowed = {(intent["journal_size"], intent["journal_sha256"])}
                            self._parse_journal(payload, quarantine_name=quarantine_name)
                        else:
                            allowed = {
                                (
                                    intent["journal_partial_size"],
                                    intent["journal_partial_sha256"],
                                ),
                                (intent["journal_size"], intent["journal_sha256"]),
                            }
                        if (len(payload), digest) not in allowed:
                            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                        self._unlink_identity(
                            wrapper_fd,
                            journal_name,
                            identity,
                            expected_digest=digest,
                            expected_size=len(payload),
                        )
                finally:
                    os.close(wrapper_fd)
                self._rmdir_identity(management.quarantine, stage_name, wrapper_identity)
            elif quarantine is not None:
                if (
                    ready is None
                    or quarantine.mode_type != stat.S_IFDIR
                    or quarantine.identity != (ready["stage_dev"], ready["stage_ino"])
                ):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                wrapper_fd, _ = _open_directory(management.quarantine, quarantine_name)
                try:
                    if set(_bounded_names(wrapper_fd, 2)) != {"journal.json"}:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    journal_payload, _ = _read_file(
                        wrapper_fd, "journal.json", max_bytes=intent["journal_size"]
                    )
                    if (
                        len(journal_payload) != intent["journal_size"]
                        or hashlib.sha256(journal_payload).hexdigest() != intent["journal_sha256"]
                    ):
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    self._parse_journal(journal_payload, quarantine_name=quarantine_name)
                finally:
                    os.close(wrapper_fd)
            if ready is not None:
                assert ready_payload is not None and ready_identity is not None
                self._retire_record(
                    management.quarantine,
                    record_name=ready_name,
                    retired_name=ready["retired_name"],
                    payload=ready_payload,
                    identity=ready_identity,
                )
            self._retire_record(
                management.quarantine,
                record_name=intent_name,
                retired_name=intent["retired_name"],
                payload=intent_payload,
                identity=intent_identity,
            )
        for name in names:
            if _STAGE_READY_NAME.fullmatch(name) is not None and name not in recovered_ready:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

    def _recover_quarantines(self, management: _Management) -> None:
        self._recover_delete_transitions(management.quarantine)
        self._recover_quarantine_record_temporaries(management)
        names = _bounded_names(management.quarantine, _MAX_QUARANTINE_ENTRIES)
        self._recover_closing_intents(management, names)
        names = _bounded_names(management.quarantine, _MAX_QUARANTINE_ENTRIES)
        self._recover_closings(management, names)
        names = _bounded_names(management.quarantine, _MAX_QUARANTINE_ENTRIES)
        self._recover_stage_intents(management, names)
        names = _bounded_names(management.quarantine, _MAX_QUARANTINE_ENTRIES)
        if any(
            _QUARANTINE_STAGE_NAME.fullmatch(name) is not None
            or _STAGE_INTENT_NAME.fullmatch(name) is not None
            or _STAGE_READY_NAME.fullmatch(name) is not None
            or _CLOSING_INTENT_NAME.fullmatch(name) is not None
            or _STAGE_INTENT_TEMP_NAME.fullmatch(name) is not None
            or _STAGE_READY_TEMP_NAME.fullmatch(name) is not None
            or _CLOSING_INTENT_TEMP_NAME.fullmatch(name) is not None
            or _CLOSING_PARTIAL_NAME.fullmatch(name) is not None
            for name in names
        ):
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        for quarantine_name in names:
            if _QUARANTINE_NAME.fullmatch(quarantine_name) is None:
                continue
            try:
                wrapper_fd, _ = _open_directory(management.quarantine, quarantine_name)
            except (FileNotFoundError, ForgeException):
                continue
            try:
                self._recover_delete_tree(wrapper_fd)
                try:
                    journal_payload, _ = _read_file(
                        wrapper_fd, "journal.json", max_bytes=_MAX_OWNER_BYTES
                    )
                except (FileNotFoundError, ForgeException):
                    continue
                generation_identity, audit = self._parse_journal(
                    journal_payload, quarantine_name=quarantine_name
                )
                try:
                    os.stat("payload", dir_fd=wrapper_fd, follow_symlinks=False)
                except FileNotFoundError:
                    try:
                        verified, _ = _read_file(
                            wrapper_fd, "verified.json", max_bytes=_MAX_OWNER_BYTES
                        )
                    except FileNotFoundError:
                        journal, _ = _read_file(
                            wrapper_fd, "journal.json", max_bytes=_MAX_OWNER_BYTES
                        )
                        if journal != journal_payload:
                            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
                        self._close_wrapper(
                            management,
                            quarantine_name=quarantine_name,
                            wrapper_fd=wrapper_fd,
                            journal_payload=journal_payload,
                            verified_payload=self._verified_payload(journal_payload),
                        )
                        continue
                    if verified != self._verified_payload(journal_payload):
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION) from None
                self._finish_quarantine(
                    management,
                    quarantine_name,
                    wrapper_fd,
                    generation_identity,
                    audit,
                    journal_payload,
                )
            finally:
                os.close(wrapper_fd)

    def _remove_owned_generation(
        self,
        management: _Management,
        book_fd: int,
        generation_name: str,
        manifest: AgentSkillManifest,
    ) -> None:
        _, _, generation_identity = self._validate_generation(
            book_fd,
            generation_name,
            expected_book_key=manifest.book_key,
            expected_slug=manifest.skill_slug,
        )
        self._checkpoint("cleanup:generation")
        generation_fd, opened_identity = _open_directory(book_fd, generation_name)
        try:
            if opened_identity != generation_identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            audit = self._audit_tree(generation_fd)
            expected_files = {_OWNER, *(f"{_CONTENT}/{item.path}" for item in manifest.files)}
            expected_files.add(f"{_CONTENT}/.cove-book-forge.json")
            expected_directories = {_CONTENT}
            for path in expected_files - {_OWNER}:
                components = path.split("/")[:-1]
                expected_directories.update(
                    "/".join(components[: index + 1]) for index in range(len(components))
                )
            expected_tree = {
                **{path: False for path in expected_files},
                **{path: True for path in expected_directories},
            }
            if set(audit) != set(expected_tree) or any(
                audit[path].directory != expected_tree[path] for path in audit
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        finally:
            os.close(generation_fd)

        wrapper_id = uuid4().hex
        staging_name = f"stage-{wrapper_id}"
        quarantine_name = f"q-{wrapper_id}"
        journal_payload = self._journal_payload(
            quarantine_name=quarantine_name,
            generation_name=generation_name,
            generation_identity=generation_identity,
            manifest=manifest,
            audit=audit,
        )
        intent_name = f"stage-intent-{wrapper_id}.json"
        retired_intent_name = f"retired-stage-intent-{wrapper_id}.json"
        intent_payload = self._stage_record_payload(
            identifier=wrapper_id,
            journal_payload=journal_payload,
            stage_identity=None,
            intent_checksum=None,
        )
        intent_identity = self._publish_record(
            management.quarantine,
            temporary_name=f".stage-intent-{wrapper_id}.tmp",
            record_name=intent_name,
            payload=intent_payload,
        )
        os.mkdir(staging_name, 0o700, dir_fd=management.quarantine)
        _fsync_directory(management.quarantine)
        wrapper_fd, wrapper_identity = _open_directory(management.quarantine, staging_name)
        ready_name = f"stage-ready-{wrapper_id}.json"
        retired_ready_name = f"retired-stage-ready-{wrapper_id}.json"
        intent = _load_checksummed_record(intent_payload)
        ready_payload = self._stage_record_payload(
            identifier=wrapper_id,
            journal_payload=journal_payload,
            stage_identity=wrapper_identity,
            intent_checksum=intent["checksum"],
        )
        ready_identity = self._publish_record(
            management.quarantine,
            temporary_name=f".stage-ready-{wrapper_id}.tmp",
            record_name=ready_name,
            payload=ready_payload,
        )
        try:
            self._checkpoint("cleanup:stage-created")
            self._write_interrupted_file(
                wrapper_fd,
                partial_name="journal.partial",
                final_name="journal.json",
                payload=journal_payload,
                checkpoint="cleanup:journal-writing",
            )
            self._checkpoint("cleanup:wrapper-staged")
            if not _rename_noreplace(
                staging_name,
                quarantine_name,
                source_fd=management.quarantine,
                destination_fd=management.quarantine,
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            _fsync_directory(management.quarantine)
            if _raw_entry(management.quarantine, quarantine_name) != _RawEntry(
                stat.S_IFDIR, wrapper_identity, None
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._retire_record(
                management.quarantine,
                record_name=ready_name,
                retired_name=retired_ready_name,
                payload=ready_payload,
                identity=ready_identity,
            )
            self._retire_record(
                management.quarantine,
                record_name=intent_name,
                retired_name=retired_intent_name,
                payload=intent_payload,
                identity=intent_identity,
            )
            self._checkpoint("cleanup:wrapper-published")
            self._checkpoint("cleanup:quarantine")
            if not _rename_noreplace(
                generation_name,
                "payload",
                source_fd=book_fd,
                destination_fd=wrapper_fd,
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            _fsync_directory(book_fd)
            _fsync_directory(wrapper_fd)
            _fsync_directory(management.quarantine)
            if _raw_entry(book_fd, generation_name) is not None:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            payload_fd, payload_identity = _open_directory(wrapper_fd, "payload")
            try:
                if payload_identity != generation_identity:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                self._audit_payload(payload_fd, audit, allow_missing=False)
            finally:
                os.close(payload_fd)
            self._finish_quarantine(
                management,
                quarantine_name,
                wrapper_fd,
                generation_identity,
                audit,
                journal_payload,
            )
        finally:
            os.close(wrapper_fd)
        _fsync_directory(book_fd)

    def _cleanup_generations(
        self, management: _Management, manifest: AgentSkillManifest, active_target: str
    ) -> None:
        _, active_name = self._target_parts(active_target)
        state = self._read_state(
            management,
            book_key=manifest.book_key,
            skill_slug=manifest.skill_slug,
        )
        if state is None or state[0].current_target != active_target:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        previous_name: str | None = None
        if state[0].previous_target is not None:
            _, previous_name = self._target_parts(state[0].previous_target)
            previous_manifest, _ = self._validate_target(
                management,
                state[0].previous_target,
                expected_slug=manifest.skill_slug,
            )
            if previous_manifest.book_key != manifest.book_key:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        book_fd, _ = _open_directory(management.generations, manifest.book_key)
        try:
            candidates: list[tuple[str, AgentSkillManifest]] = []
            for name in _bounded_names(book_fd, _MAX_TRANSACTION_COUNT):
                if _GENERATION_NAME.fullmatch(name) is None:
                    continue
                candidate, _, _ = self._validate_generation(
                    book_fd,
                    name,
                    expected_book_key=manifest.book_key,
                    expected_slug=manifest.skill_slug,
                )
                candidates.append((name, candidate))
            keep = {active_name}
            if previous_name is not None:
                keep.add(previous_name)
            for name, candidate in candidates:
                if name not in keep:
                    self._remove_owned_generation(management, book_fd, name, candidate)
        finally:
            os.close(book_fd)
