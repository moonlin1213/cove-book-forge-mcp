import os
import sqlite3
import stat
from enum import StrEnum
from importlib import import_module
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cove_book_forge.config import AppConfig, library_data_path, load_config
from cove_book_forge.config.paths import AuthorizedPathPolicy
from cove_book_forge.errors import ForgeException


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class DoctorCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    status: CheckStatus
    message: str = Field(min_length=1, max_length=1200)


class DoctorReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.status is not CheckStatus.FAIL for check in self.checks)


def _directory_check(name: str, path: Path) -> DoctorCheck:
    try:
        status = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return DoctorCheck(name=name, status=CheckStatus.FAIL, message="Directory is missing.")
    except OSError:
        return DoctorCheck(name=name, status=CheckStatus.FAIL, message="Directory is unavailable.")
    if not stat.S_ISDIR(status.st_mode):
        return DoctorCheck(name=name, status=CheckStatus.FAIL, message="Directory is unavailable.")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return DoctorCheck(name=name, status=CheckStatus.FAIL, message="Directory is not writable.")
    return DoctorCheck(name=name, status=CheckStatus.PASS, message="Directory is ready.")


def _nearest_existing_parent(path: Path) -> Path | None:
    candidate = path.parent
    while True:
        try:
            status = candidate.stat(follow_symlinks=False)
        except FileNotFoundError:
            parent = candidate.parent
            if parent == candidate:
                return None
            candidate = parent
            continue
        except OSError:
            return None
        if not stat.S_ISDIR(status.st_mode):
            return None
        return candidate


def _library_directory_check(path: Path, *, enabled: bool) -> DoctorCheck:
    try:
        status = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        parent = _nearest_existing_parent(path)
        parent_ready = parent is not None and os.access(parent, os.W_OK | os.X_OK)
        if enabled or not parent_ready:
            return DoctorCheck(
                name="library_data",
                status=CheckStatus.FAIL,
                message="Directory is missing.",
            )
        return DoctorCheck(
            name="library_data",
            status=CheckStatus.WARN,
            message="Optional library directory does not exist; its parent is ready.",
        )
    except OSError:
        return DoctorCheck(
            name="library_data",
            status=CheckStatus.FAIL,
            message="Directory is unavailable.",
        )
    if not stat.S_ISDIR(status.st_mode):
        return DoctorCheck(
            name="library_data",
            status=CheckStatus.FAIL,
            message="Directory is unavailable.",
        )
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        return DoctorCheck(
            name="library_data",
            status=CheckStatus.FAIL,
            message="Directory is not writable.",
        )
    return DoctorCheck(
        name="library_data",
        status=CheckStatus.PASS,
        message="Directory is ready.",
    )


def _dependency_check(name: str, module: str) -> DoctorCheck:
    try:
        import_module(module)
    except Exception:
        return DoctorCheck(
            name=name,
            status=CheckStatus.FAIL,
            message="Parser dependency is unavailable.",
        )
    return DoctorCheck(
        name=name,
        status=CheckStatus.PASS,
        message="Parser dependency is available.",
    )


def _database_check(data_path: Path, *, enabled: bool) -> DoctorCheck:
    database = data_path / "library.sqlite3"
    try:
        status = database.stat(follow_symlinks=False)
    except FileNotFoundError:
        if enabled and _library_directory_check(data_path, enabled=True).status is CheckStatus.PASS:
            return DoctorCheck(
                name="library_database",
                status=CheckStatus.PASS,
                message="Database is ready to initialize.",
            )
        return DoctorCheck(
            name="library_database",
            status=CheckStatus.WARN if not enabled else CheckStatus.FAIL,
            message="Optional database does not exist."
            if not enabled
            else "Database is unavailable.",
        )
    except OSError:
        return DoctorCheck(
            name="library_database",
            status=CheckStatus.FAIL,
            message="Database is unavailable.",
        )
    if not stat.S_ISREG(status.st_mode):
        return DoctorCheck(
            name="library_database",
            status=CheckStatus.FAIL,
            message="Database is not a regular file.",
        )
    try:
        uri = f"{database.absolute().as_uri()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True, timeout=0.0) as connection:
            result = connection.execute("PRAGMA quick_check(1)").fetchone()
        if result is None or result[0] != "ok":
            raise sqlite3.DatabaseError("integrity check failed")
    except (OSError, sqlite3.Error, ValueError):
        return DoctorCheck(
            name="library_database",
            status=CheckStatus.FAIL,
            message="Database failed its read-only readiness check.",
        )
    return DoctorCheck(
        name="library_database",
        status=CheckStatus.PASS,
        message="Database passed its read-only readiness check.",
    )


def _authorized_directory_check(name: str, path: Path) -> DoctorCheck:
    directory_check = _directory_check(name, path)
    if directory_check.status is CheckStatus.FAIL:
        return directory_check
    try:
        AuthorizedPathPolicy((path,))
    except ForgeException as exc:
        return DoctorCheck(name=name, status=CheckStatus.FAIL, message=str(exc))
    return DoctorCheck(name=name, status=CheckStatus.PASS, message="Directory is ready.")


def _checks_for_config(config: AppConfig) -> list[DoctorCheck]:
    checks = [
        DoctorCheck(
            name="configuration",
            status=CheckStatus.PASS,
            message="Configuration loaded.",
        )
    ]
    checks.extend(
        (
            _dependency_check("beautifulsoup4", "bs4"),
            _dependency_check("defusedxml", "defusedxml"),
            _dependency_check("pypdf", "pypdf"),
        )
    )
    key_name = config.model.api_key_env
    if key_name:
        is_set = bool(os.environ.get(key_name))
        checks.append(
            DoctorCheck(
                name="model_api_key",
                status=CheckStatus.PASS if is_set else CheckStatus.FAIL,
                message=f"Environment variable is {'set' if is_set else 'missing'}: {key_name}",
            )
        )
    data_path = library_data_path(config)
    checks.append(_library_directory_check(data_path, enabled=config.library.enabled))
    checks.append(_database_check(data_path, enabled=config.library.enabled))
    if config.outputs.obsidian.enabled and config.outputs.obsidian.vault_path is not None:
        checks.append(
            _authorized_directory_check("obsidian_vault", config.outputs.obsidian.vault_path)
        )
    if config.outputs.skills.enabled and config.outputs.skills.canonical_path is not None:
        checks.append(
            _authorized_directory_check("skill_root", config.outputs.skills.canonical_path)
        )
    return checks


def run_doctor(config_path: Path | None = None) -> DoctorReport:
    try:
        config = load_config(config_path)
    except ForgeException as exc:
        return DoctorReport(
            checks=(DoctorCheck(name="configuration", status=CheckStatus.FAIL, message=str(exc)),)
        )
    return DoctorReport(checks=tuple(_checks_for_config(config)))
