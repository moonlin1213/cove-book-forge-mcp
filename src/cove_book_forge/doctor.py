import os
from enum import StrEnum
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
    if not path.exists() or not path.is_dir():
        return DoctorCheck(name=name, status=CheckStatus.FAIL, message="Directory is missing.")
    if not os.access(path, os.W_OK):
        return DoctorCheck(name=name, status=CheckStatus.FAIL, message="Directory is not writable.")
    return DoctorCheck(name=name, status=CheckStatus.PASS, message="Directory is ready.")


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
    if config.library.enabled:
        checks.append(_directory_check("library_data", library_data_path(config)))
    if config.outputs.obsidian.enabled and config.outputs.obsidian.vault_path is not None:
        checks.append(
            _authorized_directory_check("obsidian_vault", config.outputs.obsidian.vault_path)
        )
    if config.outputs.skills.enabled and config.outputs.skills.canonical_path is not None:
        checks.append(_authorized_directory_check("skill_root", config.outputs.skills.canonical_path))
    return checks


def run_doctor(config_path: Path | None = None) -> DoctorReport:
    try:
        config = load_config(config_path)
    except ForgeException as exc:
        return DoctorReport(
            checks=(DoctorCheck(name="configuration", status=CheckStatus.FAIL, message=str(exc)),)
        )
    return DoctorReport(checks=tuple(_checks_for_config(config)))
