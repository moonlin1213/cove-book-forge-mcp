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
_MAX_TREE_FILES: Final = 5_011
_MAX_TREE_ENTRIES: Final = 10_024
_MAX_TRANSACTION_COUNT: Final = 64
_MAX_ACTIVATION_ENTRIES: Final = 128
_READ_CHUNK: Final = 1024 * 1024
_TX_NAME = re.compile(r"^tx-([0-9a-f]{32})$")
_GENERATION_NAME = re.compile(r"^gen-([0-9a-f]{64})$")
_ACTIVATION_NAME = re.compile(r"^activate-([0-9a-f]{32})$")
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
    if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
    descriptor = os.open(name, _READ_FLAGS, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
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
        if _identity(after) != _identity(opened) or after.st_size != total:
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
        return _identity(os.fstat(descriptor))
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

    def close(self) -> None:
        for descriptor in (self.activations, self.transactions, self.generations, self.root):
            with suppress(OSError):
                os.close(descriptor)


@dataclass(frozen=True)
class _ActiveGeneration:
    target: str
    pointer_identity: _Identity
    manifest: AgentSkillManifest
    files: Mapping[str, bytes]


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
            self._recover_transactions(management)
            self._recover_activations(anchor, management)
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
                return SkillPublisherReceipt(render, render.skill_slug, (), True)

            target = self._ensure_generation(management, render, plan.complete_files)
            self._activate(anchor, management, render, target, active)
            self._checkpoint("manifest:switch")
            visible = self._active_generation(anchor, management, render.skill_slug)
            if visible is None or visible.manifest != render.manifest or visible.target != target:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._checkpoint("cleanup:start")
            self._cleanup_generations(management, render.manifest, target)
            anchor.verify()
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
        try:
            names = set(_bounded_names(root, 8))
            if created:
                _write_file(root, _OWNER, _ROOT_OWNER_BYTES)
                _fsync_directory(root)
                names.add(_OWNER)
            owner, _ = _read_file(root, _OWNER, max_bytes=len(_ROOT_OWNER_BYTES))
            if owner != _ROOT_OWNER_BYTES:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            allowed = {_OWNER, "generations", "transactions", "activations"}
            if not names <= allowed:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            generations, _, _ = _create_or_open_directory(root, "generations")
            transactions, _, _ = _create_or_open_directory(root, "transactions")
            activations, _, _ = _create_or_open_directory(root, "activations")
            return _Management(root, generations, transactions, activations)
        except BaseException:
            os.close(root)
            raise

    def _read_tree(self, root_fd: int) -> tuple[dict[str, bytes], dict[str, str]]:
        files: dict[str, bytes] = {}
        kinds: dict[str, str] = {}
        entry_count = 0

        def visit(directory_fd: int, prefix: str) -> None:
            nonlocal entry_count
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
                    if len(files) >= _MAX_TREE_FILES:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    payload, _ = _read_file(directory_fd, name, max_bytes=_MAX_FILE_BYTES)
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
    ) -> tuple[AgentSkillManifest, dict[str, bytes]]:
        book_key, generation_name = self._target_parts(target)
        book_fd, _ = _open_directory(management.generations, book_key)
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
        manifest, files = self._validate_target(management, target, expected_slug=skill_slug)
        anchor.verify()
        current = self._active_pointer(anchor, skill_slug)
        if current != pointer:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
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

    def _unlink_identity(self, directory_fd: int, name: str, identity: _Identity) -> None:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(current) != identity:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        os.unlink(name, dir_fd=directory_fd)

    def _remove_activation_marker(self, management: _Management, name: str) -> None:
        marker_name = f"{name}.json"
        _, identity = _read_file(management.activations, marker_name, max_bytes=_MAX_OWNER_BYTES)
        self._unlink_identity(management.activations, marker_name, identity)
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
        activation, new_identity = self._create_activation(management, render, target, old_target)
        self._checkpoint("activation:before")
        anchor.verify()
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
            _rename_exchange(
                activation,
                render.skill_slug,
                source_fd=management.activations,
                destination_fd=anchor.descriptor,
            )
            _fsync_directory(anchor.descriptor)
            swapped_new = self._active_pointer(anchor, render.skill_slug)
            swapped_old = self._active_pointer_in(
                management.activations, activation, validate_target=False
            )
            if swapped_new != (target, new_identity) or swapped_old != (
                active.target,
                active.pointer_identity,
            ):
                if swapped_new == (target, new_identity):
                    _rename_exchange(
                        activation,
                        render.skill_slug,
                        source_fd=management.activations,
                        destination_fd=anchor.descriptor,
                    )
                    _fsync_directory(anchor.descriptor)
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        self._checkpoint("activation:after")
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
            new_target = marker.get("new_target")
            old_target = marker.get("old_target")
            if (
                marker.get("owner") != "cove-book-forge-skill-activation"
                or marker.get("schema") != 1
                or not isinstance(active_name, str)
                or not isinstance(new_target, str)
                or old_target is not None
                and not isinstance(old_target, str)
            ):
                continue
            self._validate_target(management, new_target, expected_slug=active_name)
            if isinstance(old_target, str):
                self._validate_target(management, old_target, expected_slug=active_name)
            root_pointer = self._active_pointer(anchor, active_name)
            staged_pointer = self._active_pointer_in(
                management.activations, activation, validate_target=True
            )
            allowed = {
                (old_target, new_target),
                (new_target, old_target),
                (new_target, None),
            }
            state = (
                root_pointer[0] if root_pointer is not None else None,
                staged_pointer[0] if staged_pointer is not None else None,
            )
            if state not in allowed:
                continue
            if staged_pointer is not None:
                self._unlink_identity(management.activations, activation, staged_pointer[1])
            self._unlink_identity(management.activations, marker_name, marker_identity)
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
                    if not set(files) <= set(expected_files):
                        if require_owned:
                            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                        return
                finally:
                    os.close(content_fd)
            snapshot = self._snapshot_tree(tx_fd)
            allowed_directories = {_CONTENT}
            allowed_files = {_OWNER}
            for path in expected_files:
                managed_path = f"{_CONTENT}/{path}"
                allowed_files.add(managed_path)
                components = managed_path.split("/")[:-1]
                allowed_directories.update(
                    "/".join(components[: index + 1]) for index in range(len(components))
                )
            if any(
                path not in allowed_directories | allowed_files
                or is_directory != (path in allowed_directories)
                for path, (is_directory, _) in snapshot.items()
            ):
                if require_owned:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                return
            if snapshot.get(_OWNER) != (False, owner_identity) or (
                _CONTENT in names and snapshot.get(_CONTENT) != (True, content_identity)
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._remove_tree_snapshot(tx_fd, snapshot)
        finally:
            os.close(tx_fd)
        current_tx = os.stat(name, dir_fd=management.transactions, follow_symlinks=False)
        if _identity(current_tx) != tx_identity:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        os.rmdir(name, dir_fd=management.transactions)
        _fsync_directory(management.transactions)

    def _snapshot_tree(self, directory_fd: int) -> dict[str, tuple[bool, _Identity]]:
        snapshot: dict[str, tuple[bool, _Identity]] = {}

        def visit(current_fd: int, prefix: str) -> None:
            remaining = _MAX_TREE_ENTRIES - len(snapshot)
            for name in _bounded_names(current_fd, remaining):
                path = f"{prefix}/{name}" if prefix else name
                entry = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
                identity = _identity(entry)
                if stat.S_ISDIR(entry.st_mode):
                    snapshot[path] = (True, identity)
                    child, opened_identity = _open_directory(current_fd, name)
                    try:
                        if opened_identity != identity:
                            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                        visit(child, path)
                    finally:
                        os.close(child)
                elif stat.S_ISREG(entry.st_mode):
                    snapshot[path] = (False, identity)
                else:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)

        visit(directory_fd, "")
        return snapshot

    def _remove_tree_snapshot(
        self,
        directory_fd: int,
        snapshot: Mapping[str, tuple[bool, _Identity]],
        prefix: str = "",
    ) -> None:
        children = {
            path.removeprefix(f"{prefix}/") if prefix else path
            for path in snapshot
            if (not prefix or path.startswith(f"{prefix}/"))
            and "/" not in (path.removeprefix(f"{prefix}/") if prefix else path)
        }
        if set(_bounded_names(directory_fd, _MAX_TREE_ENTRIES)) != children:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        for name in sorted(children):
            path = f"{prefix}/{name}" if prefix else name
            is_directory, identity = snapshot[path]
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if _identity(current) != identity:
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            if is_directory:
                if not stat.S_ISDIR(current.st_mode):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                child, opened_identity = _open_directory(directory_fd, name)
                try:
                    if opened_identity != identity:
                        raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                    self._remove_tree_snapshot(child, snapshot, path)
                finally:
                    os.close(child)
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if _identity(current) != identity:
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                os.rmdir(name, dir_fd=directory_fd)
            else:
                if not stat.S_ISREG(current.st_mode):
                    raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
                self._unlink_identity(directory_fd, name, identity)
        _fsync_directory(directory_fd)

    def _remove_owned_generation(
        self, book_fd: int, generation_name: str, manifest: AgentSkillManifest
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
            snapshot = self._snapshot_tree(generation_fd)
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
            if set(snapshot) != set(expected_tree) or any(
                snapshot[path][0] != expected_tree[path] for path in snapshot
            ):
                raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
            self._remove_tree_snapshot(generation_fd, snapshot)
        finally:
            os.close(generation_fd)
        current = os.stat(generation_name, dir_fd=book_fd, follow_symlinks=False)
        if _identity(current) != generation_identity:
            raise _error(ForgeErrorCode.EXTERNAL_MODIFICATION)
        os.rmdir(generation_name, dir_fd=book_fd)
        _fsync_directory(book_fd)

    def _cleanup_generations(
        self, management: _Management, manifest: AgentSkillManifest, active_target: str
    ) -> None:
        _, active_name = self._target_parts(active_target)
        book_fd, _ = _open_directory(management.generations, manifest.book_key)
        try:
            candidates: list[tuple[int, str, AgentSkillManifest]] = []
            for name in _bounded_names(book_fd, _MAX_TRANSACTION_COUNT):
                if _GENERATION_NAME.fullmatch(name) is None:
                    continue
                try:
                    candidate, _, _ = self._validate_generation(
                        book_fd,
                        name,
                        expected_book_key=manifest.book_key,
                        expected_slug=manifest.skill_slug,
                    )
                    entry = os.stat(name, dir_fd=book_fd, follow_symlinks=False)
                except ForgeException:
                    continue
                candidates.append((entry.st_mtime_ns, name, candidate))
            previous = sorted(
                (item for item in candidates if item[1] != active_name), reverse=True
            )[:1]
            keep = {active_name, *(item[1] for item in previous)}
            for _, name, candidate in candidates:
                if name not in keep:
                    self._remove_owned_generation(book_fd, name, candidate)
        finally:
            os.close(book_fd)
