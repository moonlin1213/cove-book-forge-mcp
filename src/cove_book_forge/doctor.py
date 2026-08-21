import os
import sqlite3
import stat
from contextlib import closing, suppress
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cove_book_forge.config import AppConfig, library_data_path, load_config
from cove_book_forge.config.paths import AuthorizedPathPolicy
from cove_book_forge.errors import ForgeException
from cove_book_forge.library.database import _inspect_library_schema, _LibrarySchemaReadiness
from cove_book_forge.library.service import _validate_data_root
from cove_book_forge.outputs import skill_install as skill_install_module
from cove_book_forge.outputs import skill_publisher as skill_publisher_module
from cove_book_forge.outputs.publisher import check_obsidian_output_readiness
from cove_book_forge.outputs.skill_publisher import check_skill_output_readiness
from cove_book_forge.providers import ProviderRegistry
from cove_book_forge.providers.credentials import resolve_provider_credential

_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_LIBRARY_DATABASE_FILENAME = "library.sqlite3"
_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_DATABASE_OPEN_FLAGS = os.O_RDONLY | os.O_NOFOLLOW
# In-memory SQLite deserialize may require additional working memory beyond this payload.
_MAX_DATABASE_SNAPSHOT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _FileStatus:
    device: int
    inode: int
    mode: int
    size: int
    modified_ns: int
    changed_ns: int

    @classmethod
    def capture(cls, status: os.stat_result) -> "_FileStatus":
        return cls(
            device=status.st_dev,
            inode=status.st_ino,
            mode=status.st_mode,
            size=status.st_size,
            modified_ns=status.st_mtime_ns,
            changed_ns=status.st_ctime_ns,
        )


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
        validated = _validate_data_root(path)
    except ForgeException:
        return DoctorCheck(
            name="library_data",
            status=CheckStatus.FAIL,
            message="Directory is not allowed.",
        )
    try:
        status = validated.stat(follow_symlinks=False)
    except FileNotFoundError:
        parent = _nearest_existing_parent(validated)
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
    if not os.access(validated, os.R_OK | os.W_OK | os.X_OK):
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


def _model_provider_check(config: AppConfig) -> DoctorCheck:
    model_config = config.model
    try:
        ProviderRegistry().create(model_config)
    except Exception:
        return DoctorCheck(
            name="model_provider",
            status=CheckStatus.FAIL,
            message="Model provider configuration is unavailable.",
        )
    return DoctorCheck(
        name="model_provider",
        status=CheckStatus.PASS,
        message="Model provider configuration is ready.",
    )


def _model_api_key_check(config: AppConfig) -> DoctorCheck | None:
    model_config = config.model
    key_name = model_config.api_key_env
    try:
        resolve_provider_credential(model_config)
    except ForgeException:
        if key_name is not None:
            return DoctorCheck(
                name="model_api_key",
                status=CheckStatus.FAIL,
                message=f"Environment variable is missing: {key_name}",
            )
        return DoctorCheck(
            name="model_api_key",
            status=CheckStatus.FAIL,
            message="API key environment variable is not configured.",
        )
    if key_name is None:
        return None
    return DoctorCheck(
        name="model_api_key",
        status=CheckStatus.PASS,
        message=f"Environment variable is set: {key_name}",
    )


def _sqlite_sidecars_absent(directory_fd: int) -> bool:
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        try:
            os.stat(
                f"{_LIBRARY_DATABASE_FILENAME}{suffix}",
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        except OSError:
            return False
        return False
    return True


def _sidecar_failure() -> DoctorCheck:
    return DoctorCheck(
        name="library_database",
        status=CheckStatus.FAIL,
        message="Database cannot be inspected read-only while SQLite sidecars exist.",
    )


def _root_is_stable(
    data_path: Path,
    validated: Path,
    directory_fd: int,
    expected: _FileStatus,
) -> bool:
    try:
        if _validate_data_root(data_path) != validated:
            return False
        held_status = os.fstat(directory_fd)
        public_status = validated.stat(follow_symlinks=False)
    except (ForgeException, OSError):
        return False
    return (
        stat.S_ISDIR(held_status.st_mode)
        and stat.S_ISDIR(public_status.st_mode)
        and _FileStatus.capture(held_status) == expected
        and _FileStatus.capture(public_status) == expected
    )


def _database_is_stable(
    directory_fd: int,
    database_fd: int,
    expected: _FileStatus,
) -> bool:
    try:
        held_status = os.fstat(database_fd)
        relative_status = os.stat(
            _LIBRARY_DATABASE_FILENAME,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError:
        return False
    return (
        stat.S_ISREG(held_status.st_mode)
        and stat.S_ISREG(relative_status.st_mode)
        and _FileStatus.capture(held_status) == expected
        and _FileStatus.capture(relative_status) == expected
    )


def _missing_database_is_stable(
    data_path: Path,
    validated: Path,
    directory_fd: int,
    root_status: _FileStatus,
) -> bool:
    if not _sqlite_sidecars_absent(directory_fd):
        return False
    try:
        os.stat(
            _LIBRARY_DATABASE_FILENAME,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return _root_is_stable(data_path, validated, directory_fd, root_status)
    except OSError:
        return False
    return False


def _read_descriptor(descriptor: int) -> bytearray:
    payload = bytearray()
    while chunk := os.read(
        descriptor,
        min(1024 * 1024, _MAX_DATABASE_SNAPSHOT_BYTES - len(payload) + 1),
    ):
        payload.extend(chunk)
        if len(payload) > _MAX_DATABASE_SNAPSHOT_BYTES:
            raise ValueError("database snapshot exceeds the in-memory limit")
    return payload


def _open_validated_data_root(data_path: Path) -> tuple[Path, int, _FileStatus]:
    validated = _validate_data_root(data_path)
    directory_fd = os.open(validated, _DIRECTORY_OPEN_FLAGS)
    try:
        status = os.fstat(directory_fd)
        if not stat.S_ISDIR(status.st_mode):
            raise OSError("library data root is not a directory")
    except BaseException:
        with suppress(OSError):
            os.close(directory_fd)
        raise
    return validated, directory_fd, _FileStatus.capture(status)


def _inspect_database_snapshot(payload: bytes | bytearray) -> _LibrarySchemaReadiness:
    with closing(sqlite3.connect(":memory:")) as connection:
        if payload:
            connection.deserialize(payload)
        return _inspect_library_schema(connection)


def _database_unavailable() -> DoctorCheck:
    return DoctorCheck(
        name="library_database",
        status=CheckStatus.FAIL,
        message="Database is unavailable.",
    )


def _database_check(data_path: Path, *, enabled: bool) -> DoctorCheck:
    directory_fd: int | None = None
    database_fd: int | None = None
    try:
        validated, directory_fd, root_status = _open_validated_data_root(data_path)
    except FileNotFoundError:
        return DoctorCheck(
            name="library_database",
            status=CheckStatus.WARN if not enabled else CheckStatus.FAIL,
            message="Optional database does not exist."
            if not enabled
            else "Database is unavailable.",
        )
    except (ForgeException, OSError):
        return _database_unavailable()

    try:
        if not _root_is_stable(data_path, validated, directory_fd, root_status):
            return _database_unavailable()
        if not _sqlite_sidecars_absent(directory_fd):
            return _sidecar_failure()

        try:
            relative_status = os.stat(
                _LIBRARY_DATABASE_FILENAME,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if not _missing_database_is_stable(data_path, validated, directory_fd, root_status):
                return _database_unavailable()
            if enabled:
                return DoctorCheck(
                    name="library_database",
                    status=CheckStatus.PASS,
                    message="Database is ready to initialize.",
                )
            return DoctorCheck(
                name="library_database",
                status=CheckStatus.WARN,
                message="Optional database does not exist.",
            )
        except OSError:
            return _database_unavailable()

        if not stat.S_ISREG(relative_status.st_mode):
            return DoctorCheck(
                name="library_database",
                status=CheckStatus.FAIL,
                message="Database is not a regular file.",
            )
        expected_database_status = _FileStatus.capture(relative_status)
        database_fd = os.open(
            _LIBRARY_DATABASE_FILENAME,
            _DATABASE_OPEN_FLAGS,
            dir_fd=directory_fd,
        )
        opened_database_status = os.fstat(database_fd)
        if (
            not stat.S_ISREG(opened_database_status.st_mode)
            or _FileStatus.capture(opened_database_status) != expected_database_status
        ):
            return _database_unavailable()
        if opened_database_status.st_size > _MAX_DATABASE_SNAPSHOT_BYTES:
            return _database_unavailable()
        if not _database_is_stable(directory_fd, database_fd, expected_database_status):
            return _database_unavailable()

        payload = _read_descriptor(database_fd)
        readiness = _inspect_database_snapshot(payload)

        if not _sqlite_sidecars_absent(directory_fd):
            return _sidecar_failure()
        if not _database_is_stable(directory_fd, database_fd, expected_database_status):
            return _database_unavailable()
        if not _root_is_stable(data_path, validated, directory_fd, root_status):
            return _database_unavailable()

        if readiness is _LibrarySchemaReadiness.UNINITIALIZED:
            return DoctorCheck(
                name="library_database",
                status=CheckStatus.WARN,
                message="Database is uninitialized.",
            )
        if readiness is _LibrarySchemaReadiness.MIGRATION_PENDING:
            return DoctorCheck(
                name="library_database",
                status=CheckStatus.WARN,
                message="Database schema migration is pending.",
            )
        return DoctorCheck(
            name="library_database",
            status=CheckStatus.PASS,
            message="Database schema is ready.",
        )
    except (MemoryError, OSError, sqlite3.Error, ValueError):
        if directory_fd is not None and not _sqlite_sidecars_absent(directory_fd):
            return _sidecar_failure()
        return DoctorCheck(
            name="library_database",
            status=CheckStatus.FAIL,
            message="Database failed its read-only readiness check.",
        )
    finally:
        if database_fd is not None:
            with suppress(OSError):
                os.close(database_fd)
        if directory_fd is not None:
            with suppress(OSError):
                os.close(directory_fd)


def _skipped_database_check() -> DoctorCheck:
    return DoctorCheck(
        name="library_database",
        status=CheckStatus.FAIL,
        message="Database check skipped because the library directory is unsafe.",
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


def _obsidian_output_check(config: AppConfig) -> DoctorCheck:
    output_config = config.outputs.obsidian
    if not output_config.enabled:
        return DoctorCheck(
            name="obsidian_output",
            status=CheckStatus.WARN,
            message="Obsidian output is disabled.",
        )
    try:
        check_obsidian_output_readiness(output_config)
    except ForgeException:
        return DoctorCheck(
            name="obsidian_output",
            status=CheckStatus.FAIL,
            message="Obsidian output directory is not ready.",
        )
    return DoctorCheck(
        name="obsidian_output",
        status=CheckStatus.PASS,
        message="Obsidian output directory is ready.",
    )


def _skill_install_root_check(target: str) -> DoctorCheck:
    """Probe one conventional install root through a no-follow descriptor."""
    name = f"skill_install_{target}"
    try:
        home = Path.home()
        root = home / skill_install_module._ROOTS[target]  # type: ignore[index]
        home_status = home.lstat()
        root_status = root.lstat()
        if (
            not home.is_absolute()
            or home == Path(home.anchor)
            or not stat.S_ISDIR(home_status.st_mode)
            or stat.S_ISLNK(home_status.st_mode)
            or not stat.S_ISDIR(root_status.st_mode)
            or stat.S_ISLNK(root_status.st_mode)
        ):
            raise OSError("unsafe install root")
        descriptor = skill_publisher_module._open_absolute_directory(root)
    except (ForgeException, OSError, RuntimeError, ValueError, KeyError):
        return DoctorCheck(
            name=name, status=CheckStatus.FAIL, message="Skill installation root is not ready."
        )
    try:
        accessible = False
        probe_failed = not (
            skill_publisher_module._EFFECTIVE_ACCESS_DIR_FD_SUPPORTED
            and skill_publisher_module._EFFECTIVE_ACCESS_IDS_SUPPORTED
        )
        if not probe_failed:
            try:
                accessible = os.access(
                    ".",
                    os.R_OK | os.W_OK | os.X_OK,
                    dir_fd=descriptor,
                    effective_ids=True,
                )
            except Exception:
                probe_failed = True
        if not probe_failed and accessible:
            return DoctorCheck(
                name=name, status=CheckStatus.PASS, message="Skill installation root is ready."
            )
        return DoctorCheck(
            name=name, status=CheckStatus.FAIL, message="Skill installation root is not ready."
        )
    finally:
        with suppress(OSError):
            os.close(descriptor)


def _skill_output_checks(config: AppConfig) -> tuple[DoctorCheck, ...]:
    output_config = config.outputs.skills
    if not output_config.enabled:
        return (
            DoctorCheck(
                name="skill_output",
                status=CheckStatus.WARN,
                message="Agent Skill output is disabled.",
            ),
        )
    try:
        check_skill_output_readiness(output_config)
    except ForgeException:
        canonical_check = DoctorCheck(
            name="skill_canonical",
            status=CheckStatus.FAIL,
            message="Agent Skill canonical directory is not ready.",
        )
    else:
        canonical_check = DoctorCheck(
            name="skill_canonical",
            status=CheckStatus.PASS,
            message="Agent Skill canonical directory is ready.",
        )
    return (
        canonical_check,
        *(_skill_install_root_check(target) for target in output_config.install_to),
    )


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
    checks.append(_model_provider_check(config))
    api_key_check = _model_api_key_check(config)
    if api_key_check is not None:
        checks.append(api_key_check)
    data_path = library_data_path(config)
    directory_check = _library_directory_check(data_path, enabled=config.library.enabled)
    checks.append(directory_check)
    checks.append(
        _skipped_database_check()
        if directory_check.status is CheckStatus.FAIL
        else _database_check(data_path, enabled=config.library.enabled)
    )
    checks.append(_obsidian_output_check(config))
    checks.extend(_skill_output_checks(config))
    return checks


def run_doctor(config_path: Path | None = None) -> DoctorReport:
    try:
        config = load_config(config_path)
    except ForgeException as exc:
        return DoctorReport(
            checks=(DoctorCheck(name="configuration", status=CheckStatus.FAIL, message=str(exc)),)
        )
    return DoctorReport(checks=tuple(_checks_for_config(config)))


def run_doctor_config(config: AppConfig) -> DoctorReport:
    """Run the same read-only checks for an already validated application config."""
    return DoctorReport(checks=tuple(_checks_for_config(config)))
