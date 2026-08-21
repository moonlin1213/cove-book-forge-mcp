"""Install canonical Agent Skills into explicitly selected conventional roots."""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Final, Literal
from uuid import uuid4

from cove_book_forge.config import SkillOutputConfig
from cove_book_forge.contracts import SkillInstallResult
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs.skill_managed import validate_skill_bundle
from cove_book_forge.outputs.skill_models import SkillPublisherReceipt

InstallTarget = Literal["agents", "codex", "claude"]

_ROOTS: Final[Mapping[InstallTarget, Path]] = {
    "agents": Path(".agents/skills"),
    "codex": Path(".codex/skills"),
    "claude": Path(".claude/skills"),
}
_MARKER: Final = ".cove-book-forge-install.json"
_OWNER: Final = "cove-book-forge-skill-install"
_UNSUPPORTED_SYMLINK_ERRORS: Final = frozenset({errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS})


def _error(code: ForgeErrorCode) -> ForgeException:
    return ForgeException(code, "Agent Skill installation failed")


def _canonical_json(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _entry(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _create_symlink(target: str, destination: Path) -> None:
    os.symlink(target, destination, target_is_directory=True)


def _bundle_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    try:
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if relative == _MARKER:
                continue
            status = path.lstat()
            if stat.S_ISLNK(status.st_mode):
                raise _error(ForgeErrorCode.INSTALL_CONFLICT)
            if stat.S_ISREG(status.st_mode):
                files[relative] = path.read_bytes()
            elif not stat.S_ISDIR(status.st_mode):
                raise _error(ForgeErrorCode.INSTALL_CONFLICT)
    except ForgeException:
        raise
    except (OSError, RuntimeError, ValueError):
        raise _error(ForgeErrorCode.INSTALL_CONFLICT) from None
    validate_skill_bundle(files)
    return files


class SkillInstaller:
    """Create managed relative links, or verified copies when links are unsupported."""

    def __init__(self, config: SkillOutputConfig) -> None:
        self._config = config

    def install(self, receipt: SkillPublisherReceipt) -> tuple[SkillInstallResult, ...]:
        if not self._config.enabled or self._config.canonical_path is None:
            raise _error(ForgeErrorCode.OUTPUT_NOT_CONFIGURED)
        results: list[SkillInstallResult] = []
        for target in self._config.install_to:
            results.append(self._install_one(target, receipt))
        return tuple(results)

    def _root(self, target: InstallTarget) -> tuple[Path, Path]:
        relative_root = _ROOTS[target]
        try:
            home = Path.home()
            root = home / relative_root
            home_status = home.lstat()
            status = root.lstat()
        except (FileNotFoundError, OSError, RuntimeError):
            raise _error(ForgeErrorCode.OUTPUT_NOT_CONFIGURED) from None
        if (
            not home.is_absolute()
            or home == Path(home.anchor)
            or not stat.S_ISDIR(home_status.st_mode)
            or stat.S_ISLNK(home_status.st_mode)
            or not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
        ):
            raise _error(ForgeErrorCode.PATH_NOT_ALLOWED)
        if not os.access(root, os.R_OK | os.W_OK | os.X_OK, follow_symlinks=False):
            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED)
        return root, relative_root

    def _install_one(
        self,
        target: InstallTarget,
        receipt: SkillPublisherReceipt,
    ) -> SkillInstallResult:
        root, display_root = self._root(target)
        slug = receipt.rendered.skill_slug
        canonical_root = self._config.canonical_path
        assert canonical_root is not None
        source = canonical_root / slug
        installed = root / slug
        display_path = (display_root / slug).as_posix()
        relative_source = os.path.relpath(source, start=root)
        existing = _entry(installed)
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                try:
                    managed = os.readlink(installed) == relative_source
                except OSError:
                    managed = False
                if managed:
                    return SkillInstallResult(
                        target=target,
                        path=display_path,
                        strategy="symlink",
                        unchanged=True,
                    )
                raise _error(ForgeErrorCode.INSTALL_CONFLICT)
            if stat.S_ISDIR(existing.st_mode):
                return self._install_copy(target, source, installed, display_path, receipt)
            raise _error(ForgeErrorCode.INSTALL_CONFLICT)

        try:
            _create_symlink(relative_source, installed)
        except FileExistsError:
            raise _error(ForgeErrorCode.INSTALL_CONFLICT) from None
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_SYMLINK_ERRORS:
                if exc.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
                    raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
                raise _error(ForgeErrorCode.INSTALL_CONFLICT) from None
            return self._install_copy(target, source, installed, display_path, receipt)
        return SkillInstallResult(
            target=target,
            path=display_path,
            strategy="symlink",
            unchanged=False,
        )

    def _marker(self, receipt: SkillPublisherReceipt) -> bytes:
        manifest = receipt.rendered.manifest
        return _canonical_json(
            {
                "book_key": manifest.book_key,
                "manifest_checksum": manifest.checksum,
                "owner": _OWNER,
                "schema": 1,
                "skill_slug": manifest.skill_slug,
            }
        )

    def _managed_copy_unchanged(
        self,
        installed: Path,
        receipt: SkillPublisherReceipt,
    ) -> bool:
        marker_path = installed / _MARKER
        try:
            marker = marker_path.read_bytes()
        except OSError:
            raise _error(ForgeErrorCode.INSTALL_CONFLICT) from None
        expected = self._marker(receipt)
        try:
            installed_manifest = validate_skill_bundle(_bundle_files(installed))
        except ForgeException:
            raise _error(ForgeErrorCode.INSTALL_CONFLICT) from None
        try:
            parsed = json.loads(marker.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _error(ForgeErrorCode.INSTALL_CONFLICT) from None
        if (
            not isinstance(parsed, dict)
            or parsed.get("owner") != _OWNER
            or parsed.get("schema") != 1
            or parsed.get("book_key") != receipt.rendered.manifest.book_key
            or parsed.get("skill_slug") != receipt.rendered.skill_slug
            or marker != _canonical_json(parsed)
        ):
            raise _error(ForgeErrorCode.INSTALL_CONFLICT)
        return (
            installed_manifest.checksum == receipt.rendered.manifest.checksum and marker == expected
        )

    def _install_copy(
        self,
        target: InstallTarget,
        source: Path,
        installed: Path,
        display_path: str,
        receipt: SkillPublisherReceipt,
    ) -> SkillInstallResult:
        existing = _entry(installed)
        if existing is not None:
            if not stat.S_ISDIR(existing.st_mode) or stat.S_ISLNK(existing.st_mode):
                raise _error(ForgeErrorCode.INSTALL_CONFLICT)
            if self._managed_copy_unchanged(installed, receipt):
                return SkillInstallResult(
                    target=target,
                    path=display_path,
                    strategy="copy",
                    unchanged=True,
                )

        source_files = _bundle_files(source)
        stage = installed.parent / f".{installed.name}.stage-{uuid4().hex}"
        backup = installed.parent / f".{installed.name}.backup-{uuid4().hex}"
        moved_existing = False
        try:
            stage.mkdir(mode=0o700)
            for relative, payload in source_files.items():
                destination = stage / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(payload)
            (stage / _MARKER).write_bytes(self._marker(receipt))
            validate_skill_bundle(_bundle_files(stage))
            if existing is not None:
                installed.rename(backup)
                moved_existing = True
            stage.rename(installed)
        except ForgeException:
            raise
        except FileExistsError:
            raise _error(ForgeErrorCode.INSTALL_CONFLICT) from None
        except PermissionError:
            raise _error(ForgeErrorCode.OUTPUT_PERMISSION_DENIED) from None
        except OSError:
            raise _error(ForgeErrorCode.INSTALL_CONFLICT) from None
        finally:
            if _entry(stage) is not None:
                shutil.rmtree(stage, ignore_errors=True)
            if moved_existing and _entry(installed) is None and _entry(backup) is not None:
                with suppress(OSError):
                    backup.rename(installed)
        if _entry(backup) is not None:
            shutil.rmtree(backup, ignore_errors=True)
        return SkillInstallResult(
            target=target,
            path=display_path,
            strategy="copy",
            unchanged=False,
        )
