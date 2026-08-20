from __future__ import annotations

import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from cove_book_forge import doctor as doctor_module
from cove_book_forge.cli import app
from cove_book_forge.config import load_config
from cove_book_forge.doctor import CheckStatus, run_doctor
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.library import BookLibrary, LibraryDatabase
from cove_book_forge.library import database as library_database
from cove_book_forge.providers.anthropic import AnthropicProvider
from cove_book_forge.providers.factory import ProviderRegistry
from cove_book_forge.providers.openai_compatible import OpenAICompatibleProvider
from cove_book_forge.providers.transport import ProviderTransport

runner = CliRunner()


def _write_config(path: Path, data_dir: Path, *, enabled: bool) -> Path:
    path.write_text(
        f"""
library:
  enabled: {str(enabled).lower()}
  data_dir: {data_dir}
model:
  provider: openai-compatible
  model: test
  base_url: http://localhost:11434/v1
""".strip(),
        encoding="utf-8",
    )
    return path


def _filesystem_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    for path in (root, *sorted(root.rglob("*"))):
        status = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        if stat.S_ISREG(status.st_mode):
            payload: object = path.read_bytes()
        elif stat.S_ISLNK(status.st_mode):
            payload = path.readlink().as_posix()
        else:
            payload = None
        entries.append(
            (
                relative,
                stat.S_IFMT(status.st_mode),
                status.st_size,
                status.st_mtime_ns,
                payload,
            )
        )
    return tuple(entries)


def _filesystem_metadata_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    for path in (root, *sorted(root.rglob("*"))):
        status = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        entries.append(
            (
                relative,
                status.st_dev,
                status.st_ino,
                stat.S_IFMT(status.st_mode),
                status.st_size,
                status.st_mtime_ns,
                status.st_ctime_ns,
            )
        )
    return tuple(entries)


def _check(payload: dict[str, object], name: str) -> dict[str, object]:
    checks = payload["checks"]
    assert isinstance(checks, list)
    return next(check for check in checks if check["name"] == name)


def _create_library_schema(path: Path, *, version: int) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        for statement in library_database._SCHEMA_V1:  # noqa: SLF001 - canonical fixture schema
            connection.execute(statement)
        if version >= 2:
            connection.execute(
                "ALTER TABLE chapter_snapshots "
                "ADD COLUMN content_fingerprint TEXT NOT NULL DEFAULT ''"
            )
        if version == 3:
            connection.execute(library_database._SCHEMA_V3_CHAPTER_ANALYSES)
        connection.execute(f"PRAGMA user_version = {version}")


_REQUIRED_VIEW_COLUMNS = {
    "books": (
        "book_id",
        "title",
        "author",
        "language",
        "total_chapters",
        "format",
        "import_mode",
        "source_fingerprint",
        "managed_source_path",
        "reference_source_path",
        "created_at",
        "updated_at",
    ),
    "chapters": ("book_id", "chapter_index", "title", "content", "source_locator"),
    "external_sources": (
        "external_source_id",
        "book_id",
        "source_system",
        "external_book_id",
        "created_at",
        "updated_at",
    ),
    "chapter_snapshots": (
        "chapter_snapshot_id",
        "external_source_id",
        "chapter_index",
        "snapshot_json",
        "created_at",
        "updated_at",
    ),
    "chapter_analyses": (
        "source_system",
        "external_book_id",
        "chapter_index",
        "input_fingerprint",
        "analysis_json",
        "created_at",
        "updated_at",
    ),
}


def _create_required_view(
    connection: sqlite3.Connection,
    name: str,
    *,
    version: int,
) -> None:
    columns = _REQUIRED_VIEW_COLUMNS[name]
    if name == "chapter_snapshots" and version >= 2:
        columns = (*columns, "content_fingerprint")
    projection = ", ".join(f'NULL AS "{column}"' for column in columns)
    connection.execute(f'CREATE VIEW "{name}" AS SELECT {projection}')


def _database_check_from_cli(config: Path) -> tuple[object, dict[str, object], str]:
    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])
    payload = json.loads(result.stdout)
    return result, _check(payload, "library_database"), result.stdout


def test_doctor_reports_valid_local_configuration(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
library:
  enabled: true
  data_dir: {data}
model:
  provider: openai-compatible
  model: local-model
  base_url: http://localhost:11434/v1
outputs:
  obsidian:
    enabled: false
  skills:
    enabled: false
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["checks"][0]["name"] == "configuration"
    assert _check(payload, "model_provider")["status"] == "pass"
    assert _check(payload, "beautifulsoup4")["status"] == "pass"
    assert _check(payload, "defusedxml")["status"] == "pass"
    assert _check(payload, "pypdf")["status"] == "pass"
    assert _check(payload, "library_database")["status"] == "pass"


def test_doctor_fails_when_an_ingestion_dependency_cannot_be_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=True)
    real_import = doctor_module.import_module

    def import_with_missing_pypdf(name: str) -> object:
        if name == "pypdf":
            raise ImportError("private environment detail")
        return real_import(name)

    monkeypatch.setattr(doctor_module, "import_module", import_with_missing_pypdf)

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert _check(payload, "pypdf") == {
        "name": "pypdf",
        "status": "fail",
        "message": "Parser dependency is unavailable.",
    }
    assert "private environment detail" not in result.stdout


def test_doctor_reports_missing_key_environment_without_printing_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
model:
  provider: openai-compatible
  model: cloud-model
  api_key_env: MISSING_TEST_KEY
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("MISSING_TEST_KEY", raising=False)

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "MISSING_TEST_KEY" in result.stdout
    assert "Authorization" not in result.stdout


@pytest.mark.parametrize("key_value", ["", "   "])
def test_doctor_reports_empty_key_environment_as_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key_value: str,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
model:
  provider: openai-compatible
  model: cloud-model
  api_key_env: EMPTY_TEST_KEY
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMPTY_TEST_KEY", key_value)

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert _check(payload, "model_api_key") == {
        "name": "model_api_key",
        "status": "fail",
        "message": "Environment variable is missing: EMPTY_TEST_KEY",
    }


@pytest.mark.parametrize("provider_name", ["openai", "deepseek", "anthropic"])
@pytest.mark.parametrize("key_value", [None, "", "   "])
def test_doctor_requires_a_nonempty_configured_key_for_cloud_builtins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    key_value: str | None,
) -> None:
    config = tmp_path / "config.yaml"
    key_config = ""
    if key_value is not None:
        key_config = "\n  api_key_env: CLOUD_MODEL_KEY"
        monkeypatch.setenv("CLOUD_MODEL_KEY", key_value)
    config.write_text(
        f"""
model:
  provider: {provider_name}
  model: cloud-model{key_config}
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    loaded = load_config(config)
    with pytest.raises(ForgeException) as registry_error:
        ProviderRegistry().create(loaded.model)

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert _check(payload, "model_provider")["status"] == "fail"
    assert _check(payload, "model_api_key")["status"] == "fail"
    assert registry_error.value.code is ForgeErrorCode.MODEL_AUTH_FAILED
    rendered = " ".join(
        (
            result.stdout,
            str(registry_error.value),
            repr(registry_error.value.as_result()),
        )
    )
    assert provider_name not in rendered
    if key_value:
        assert key_value not in rendered


@pytest.mark.parametrize("provider_name", ["openai", "deepseek", "anthropic"])
def test_doctor_rejects_missing_cloud_credential_before_adapter_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
model:
  provider: {provider_name}
  model: private-model
""".strip(),
        encoding="utf-8",
    )
    constructor_calls = 0

    def forbidden_construction(*_args: object, **_kwargs: object) -> None:
        nonlocal constructor_calls
        constructor_calls += 1
        raise AssertionError("credential policy must run before adapter construction")

    monkeypatch.setattr(OpenAICompatibleProvider, "__init__", forbidden_construction)
    monkeypatch.setattr(AnthropicProvider, "__init__", forbidden_construction)

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert _check(payload, "model_provider")["status"] == "fail"
    assert _check(payload, "model_api_key")["status"] == "fail"
    assert constructor_calls == 0
    assert provider_name not in result.stdout
    assert "private-model" not in result.stdout


def test_doctor_allows_credential_free_openai_compatible_local_gateway(tmp_path: Path) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
library:
  enabled: false
  data_dir: {data_dir}
model:
  provider: openai-compatible
  model: local-model
  base_url: http://localhost:11434/v1
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert _check(payload, "model_provider")["status"] == "pass"
    assert not any(check["name"] == "model_api_key" for check in payload["checks"])


@pytest.mark.parametrize(
    ("provider_name", "base_url"),
    [
        ("unregistered-private-provider", None),
        ("openai-compatible", None),
        ("openai-compatible", "https://private.invalid/v1?token=secret-query"),
        ("openai", "https://private.invalid/v1#secret-fragment"),
        ("anthropic", "https://private.invalid#secret-fragment"),
    ],
)
def test_doctor_fails_invalid_provider_readiness_with_a_generic_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_name: str,
    base_url: str | None,
) -> None:
    monkeypatch.setenv("DOCTOR_MODEL_KEY", "private-doctor-key")
    base_config = f"\n  base_url: {base_url}" if base_url is not None else ""
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
model:
  provider: {provider_name}
  model: private-model
  api_key_env: DOCTOR_MODEL_KEY{base_config}
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert _check(payload, "model_provider") == {
        "name": "model_provider",
        "status": "fail",
        "message": "Model provider configuration is unavailable.",
    }
    for private_value in (
        provider_name,
        str(base_url),
        "private-doctor-key",
        "secret-query",
        "secret-fragment",
    ):
        assert private_value not in result.stdout


def test_doctor_provider_readiness_is_network_free_and_does_not_mutate_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
library:
  enabled: false
  data_dir: {data_dir}
model:
  provider: anthropic
  model: claude-local-fixture
  api_key_env: DOCTOR_READ_ONLY_KEY
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCTOR_READ_ONLY_KEY", "configured-but-never-sent")

    def forbidden_access(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("doctor attempted provider I/O")

    monkeypatch.setattr(OpenAICompatibleProvider, "generate_text", forbidden_access)
    monkeypatch.setattr(OpenAICompatibleProvider, "generate_json", forbidden_access)
    monkeypatch.setattr(OpenAICompatibleProvider, "healthcheck", forbidden_access)
    monkeypatch.setattr(AnthropicProvider, "generate_text", forbidden_access)
    monkeypatch.setattr(AnthropicProvider, "generate_json", forbidden_access)
    monkeypatch.setattr(AnthropicProvider, "healthcheck", forbidden_access)
    monkeypatch.setattr(ProviderTransport, "request", forbidden_access)
    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden_access)
    before = _filesystem_snapshot(tmp_path)
    before_metadata = _filesystem_metadata_snapshot(tmp_path)

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert _check(payload, "model_provider")["status"] == "pass"
    assert _check(payload, "model_api_key")["status"] == "pass"
    assert "configured-but-never-sent" not in result.stdout
    assert _filesystem_snapshot(tmp_path) == before
    assert _filesystem_metadata_snapshot(tmp_path) == before_metadata


def test_doctor_redacts_private_paths_from_directory_failures(tmp_path: Path) -> None:
    missing_data = tmp_path / "private-data"
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
library:
  data_dir: {missing_data}
model:
  provider: openai-compatible
  model: local-model
""".strip(),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 1
    assert str(missing_data) not in result.stdout
    payload = json.loads(result.stdout)
    assert _check(payload, "library_data")["status"] == "fail"


def test_doctor_does_not_create_optional_library_storage_or_call_initializers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "not-created" / "library"
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)

    def forbidden_initialize(_self: object) -> None:
        raise AssertionError("doctor must not initialize library storage")

    monkeypatch.setattr(BookLibrary, "initialize", forbidden_initialize)
    monkeypatch.setattr(LibraryDatabase, "initialize", forbidden_initialize)

    report = run_doctor(config)

    assert report.ok is True
    assert next(check for check in report.checks if check.name == "library_data").status is (
        CheckStatus.WARN
    )
    assert next(check for check in report.checks if check.name == "library_database").status is (
        CheckStatus.WARN
    )
    assert _filesystem_snapshot(tmp_path) == before
    assert not data_dir.exists()


def test_doctor_opens_existing_library_database_read_only_without_side_effects(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    _create_library_schema(database, version=3)
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)

    report = run_doctor(config)

    database_check = next(check for check in report.checks if check.name == "library_database")
    assert database_check.status is CheckStatus.PASS
    assert _filesystem_snapshot(tmp_path) == before
    assert not tuple(data_dir.glob("library.sqlite3-*"))


@pytest.mark.parametrize("sidecar_suffix", ["-journal", "-shm"])
def test_doctor_fails_closed_for_any_existing_sqlite_sidecar_without_writing(
    tmp_path: Path,
    sidecar_suffix: str,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    _create_library_schema(database, version=2)
    sidecar = data_dir / f"library.sqlite3{sidecar_suffix}"
    sidecar.write_bytes(b"private sidecar payload")
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)

    result, database_check, stdout = _database_check_from_cli(config)

    assert result.exit_code == 1
    assert database_check["status"] == "fail"
    assert "sidecar" in str(database_check["message"]).lower()
    assert str(data_dir) not in stdout
    assert "private sidecar payload" not in stdout
    assert _filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("enabled", [False, True])
def test_doctor_fails_closed_for_orphan_sqlite_sidecars(
    tmp_path: Path,
    sidecar_suffix: str,
    enabled: bool,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    sidecar = data_dir / f"library.sqlite3{sidecar_suffix}"
    sidecar.write_bytes(b"orphan private sidecar")
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=enabled)
    before = _filesystem_snapshot(tmp_path)

    result, database_check, stdout = _database_check_from_cli(config)

    assert result.exit_code == 1
    assert database_check["status"] == "fail"
    assert "sidecar" in str(database_check["message"]).lower()
    assert "orphan private sidecar" not in stdout
    assert str(data_dir) not in stdout
    assert _filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize("wal_state", ["committed", "truncated"])
def test_doctor_fails_closed_for_real_wal_state_without_touching_database_files(
    tmp_path: Path,
    wal_state: str,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    _create_library_schema(database, version=2)
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.execute(
            """
            INSERT INTO books (
                book_id, title, author, language, total_chapters,
                created_at, updated_at
            ) VALUES ('wal-book', 'Private WAL title', '', '', 0, 'now', 'now')
            """
        )
        connection.commit()
        wal = data_dir / "library.sqlite3-wal"
        assert wal.exists()
        if wal_state == "truncated":
            payload = wal.read_bytes()
            wal.write_bytes(payload[: max(32, len(payload) // 2)])
        config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
        before = _filesystem_snapshot(tmp_path)

        result, database_check, stdout = _database_check_from_cli(config)

        assert result.exit_code == 1
        assert database_check["status"] == "fail"
        assert "sidecar" in str(database_check["message"]).lower()
        assert "Private WAL title" not in stdout
        assert str(database) not in stdout
        assert _filesystem_snapshot(tmp_path) == before
    finally:
        connection.close()


def test_doctor_rechecks_sidecars_after_read_only_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    _create_library_schema(database, version=2)
    sidecar = data_dir / "library.sqlite3-journal"
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    real_connect = sqlite3.connect
    opened: list[SidecarAppearingConnection] = []

    class SidecarAppearingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection
            self.closed = False

        def __enter__(self) -> SidecarAppearingConnection:
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            return None

        def close(self) -> None:
            self.closed = True
            self._connection.close()

        def execute(self, statement: str, *args: Any) -> sqlite3.Cursor:
            result = self._connection.execute(statement, *args)
            if statement == "PRAGMA quick_check(1)":
                sidecar.write_bytes(b"appeared concurrently")
            return result

        def deserialize(self, data: bytes) -> None:
            self._connection.deserialize(data)

    def connect_with_concurrent_sidecar(*args: Any, **kwargs: Any) -> SidecarAppearingConnection:
        connection = SidecarAppearingConnection(real_connect(*args, **kwargs))
        opened.append(connection)
        return connection

    monkeypatch.setattr(doctor_module.sqlite3, "connect", connect_with_concurrent_sidecar)

    result, database_check, _stdout = _database_check_from_cli(config)

    assert result.exit_code == 1
    assert database_check["status"] == "fail"
    assert sidecar.read_bytes() == b"appeared concurrently"
    assert opened and opened[0].closed is True


def test_doctor_fails_if_an_external_delete_transaction_changes_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    _create_library_schema(database, version=2)
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    journal = data_dir / "library.sqlite3-journal"
    real_inspect = doctor_module._inspect_library_schema
    state_after_external_write: list[tuple[tuple[object, ...], ...]] = []

    def inspect_then_change_database(
        connection: sqlite3.Connection,
    ) -> library_database._LibrarySchemaReadiness:
        readiness = real_inspect(connection)
        with closing(sqlite3.connect(database)) as writer:
            assert writer.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("PRAGMA user_version = 999")
            assert journal.exists()
            writer.commit()
        assert not journal.exists()
        state_after_external_write.append(_filesystem_snapshot(tmp_path))
        return readiness

    monkeypatch.setattr(doctor_module, "_inspect_library_schema", inspect_then_change_database)

    result, database_check, _stdout = _database_check_from_cli(config)

    assert result.exit_code == 1
    assert database_check["status"] == "fail"
    assert state_after_external_write
    assert _filesystem_snapshot(tmp_path) == state_after_external_write[0]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999


def test_doctor_rejects_ancestor_replacement_before_any_relative_root_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured_parent = tmp_path / "configured-parent"
    data_dir = configured_parent / "library"
    data_dir.mkdir(parents=True)
    displaced_parent = tmp_path / "displaced-parent"
    private_parent = tmp_path / "private-parent"
    private_data_dir = private_parent / "library"
    private_data_dir.mkdir(parents=True)
    private_database = private_data_dir / "library.sqlite3"
    _create_library_schema(private_database, version=2)
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=True)
    private_before = _filesystem_snapshot(private_data_dir)
    private_root_status = private_data_dir.stat()
    private_root_identity = (private_root_status.st_dev, private_root_status.st_ino)
    private_database_status = private_database.stat()
    private_database_identity = (private_database_status.st_dev, private_database_status.st_ino)
    real_validate = doctor_module._validate_data_root
    real_open = os.open
    real_read = os.read
    real_stat = os.stat
    validation_count = 0
    replacement_performed = False
    private_relative_stat_requested = False
    private_database_descriptor_requested = False
    private_database_read = False

    def validate_then_replace_ancestor(path: Path) -> Path:
        nonlocal validation_count, replacement_performed
        validated = real_validate(path)
        if path == data_dir:
            validation_count += 1
            if validation_count == 2:
                configured_parent.rename(displaced_parent)
                configured_parent.symlink_to(private_parent, target_is_directory=True)
                replacement_performed = True
        return validated

    def stat_with_private_root_sentinel(*args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal private_relative_stat_requested
        directory_fd = kwargs.get("dir_fd")
        if isinstance(directory_fd, int):
            status = os.fstat(directory_fd)
            if (status.st_dev, status.st_ino) == private_root_identity:
                private_relative_stat_requested = True
        return real_stat(*args, **kwargs)

    def open_with_private_database_sentinel(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal private_database_descriptor_requested
        if dir_fd is not None:
            status = os.fstat(dir_fd)
            if (status.st_dev, status.st_ino) == private_root_identity:
                private_database_descriptor_requested = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def read_with_private_database_sentinel(descriptor: int, length: int) -> bytes:
        nonlocal private_database_read
        status = os.fstat(descriptor)
        if (status.st_dev, status.st_ino) == private_database_identity:
            private_database_read = True
        return real_read(descriptor, length)

    monkeypatch.setattr(doctor_module, "_validate_data_root", validate_then_replace_ancestor)
    monkeypatch.setattr(doctor_module.os, "stat", stat_with_private_root_sentinel)
    monkeypatch.setattr(doctor_module.os, "open", open_with_private_database_sentinel)
    monkeypatch.setattr(doctor_module.os, "read", read_with_private_database_sentinel)

    result, database_check, stdout = _database_check_from_cli(config)

    assert replacement_performed is True
    assert result.exit_code == 1
    assert database_check["status"] == "fail"
    assert private_relative_stat_requested is False
    assert private_database_descriptor_requested is False
    assert private_database_read is False
    assert str(private_data_dir) not in stdout
    assert _filesystem_snapshot(private_data_dir) == private_before


def test_doctor_binds_database_inspection_to_the_validated_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    displaced_data_dir = tmp_path / "displaced-library"
    private_data_dir = tmp_path / "private-library"
    private_data_dir.mkdir()
    _create_library_schema(private_data_dir / "library.sqlite3", version=2)
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=True)
    private_before = _filesystem_snapshot(private_data_dir)
    private_database_status = (private_data_dir / "library.sqlite3").stat()
    private_database_identity = (private_database_status.st_dev, private_database_status.st_ino)
    real_open = os.open
    real_read = os.read
    real_connect = sqlite3.connect
    replacement_performed = False
    private_database_opened = False
    private_database_read = False

    def open_then_replace_root(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replacement_performed
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if (
            not replacement_performed
            and dir_fd is None
            and os.fspath(path) == os.fspath(data_dir)
            and flags & os.O_DIRECTORY
        ):
            try:
                data_dir.rename(displaced_data_dir)
                data_dir.symlink_to(private_data_dir, target_is_directory=True)
            except BaseException:
                os.close(descriptor)
                raise
            replacement_performed = True
        return descriptor

    def read_with_private_database_sentinel(descriptor: int, length: int) -> bytes:
        nonlocal private_database_read
        status = os.fstat(descriptor)
        if (status.st_dev, status.st_ino) == private_database_identity:
            private_database_read = True
        return real_read(descriptor, length)

    def connect_with_private_database_sentinel(
        database_arg: object, *args: object, **kwargs: object
    ) -> sqlite3.Connection:
        nonlocal private_database_opened
        if database_arg != ":memory:":
            private_database_opened = True
        return real_connect(database_arg, *args, **kwargs)

    monkeypatch.setattr(doctor_module.os, "open", open_then_replace_root)
    monkeypatch.setattr(doctor_module.os, "read", read_with_private_database_sentinel)
    monkeypatch.setattr(doctor_module.sqlite3, "connect", connect_with_private_database_sentinel)

    result, database_check, stdout = _database_check_from_cli(config)

    assert replacement_performed is True
    assert result.exit_code == 1
    assert database_check["status"] == "fail"
    assert private_database_opened is False
    assert private_database_read is False
    assert str(private_data_dir) not in stdout
    assert _filesystem_snapshot(private_data_dir) == private_before


@pytest.mark.parametrize(
    "unsafe_kind",
    ["final_symlink", "symlink_ancestor", "root", "home", "non_directory_ancestor"],
)
def test_doctor_and_book_library_share_data_root_rejection_and_skip_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_kind: str,
) -> None:
    private_target = tmp_path / "private-target"
    private_target.mkdir()
    nested_target = private_target / "nested"
    nested_target.mkdir()
    target_database = nested_target / "library.sqlite3"
    _create_library_schema(target_database, version=2)

    if unsafe_kind == "final_symlink":
        data_dir = tmp_path / "linked-library"
        data_dir.symlink_to(nested_target, target_is_directory=True)
    elif unsafe_kind == "symlink_ancestor":
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(private_target, target_is_directory=True)
        data_dir = linked_parent / "nested"
    elif unsafe_kind == "root":
        data_dir = Path("/")
    elif unsafe_kind == "home":
        data_dir = Path.home()
    else:
        occupied = tmp_path / "occupied"
        occupied.write_text("private non-directory ancestor", encoding="utf-8")
        data_dir = occupied / "nested"

    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(private_target)

    with pytest.raises(ForgeException) as exc_info:
        BookLibrary(load_config(config))
    assert exc_info.value.code is ForgeErrorCode.PATH_NOT_ALLOWED

    def forbidden_connect(*_args: object, **_kwargs: object) -> sqlite3.Connection:
        raise AssertionError("doctor must skip database inspection for an unsafe data root")

    monkeypatch.setattr(doctor_module.sqlite3, "connect", forbidden_connect)

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert _check(payload, "library_data")["status"] == "fail"
    assert _check(payload, "library_database")["status"] == "fail"
    assert "skipped" in str(_check(payload, "library_database")["message"]).lower()
    assert str(private_target) not in result.stdout
    assert "private non-directory ancestor" not in result.stdout
    assert _filesystem_snapshot(private_target) == before


@pytest.mark.parametrize(
    ("case", "expected_status", "expected_fragment"),
    [
        ("v1", "warn", "migration"),
        ("v2", "warn", "migration"),
        ("v3", "pass", None),
        ("future", "fail", None),
        ("v2_missing_table", "fail", None),
        ("v2_missing_column", "fail", None),
        ("v3_missing_table", "fail", None),
        ("v3_missing_column", "fail", None),
        ("v0_unrelated", "fail", None),
        ("v0_empty", "warn", "uninitialized"),
    ],
)
def test_doctor_validates_library_application_schema_without_writing(
    tmp_path: Path,
    case: str,
    expected_status: str,
    expected_fragment: str | None,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    if case == "v1":
        _create_library_schema(database, version=1)
    elif case == "v2":
        _create_library_schema(database, version=2)
    elif case == "v3":
        _create_library_schema(database, version=3)
    elif case == "future":
        database.touch()
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("PRAGMA user_version = 999")
    elif case == "v2_missing_table":
        _create_library_schema(database, version=2)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TABLE chapters")
    elif case == "v2_missing_column":
        _create_library_schema(database, version=1)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("PRAGMA user_version = 2")
    elif case == "v3_missing_table":
        _create_library_schema(database, version=3)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("DROP TABLE chapter_analyses")
    elif case == "v3_missing_column":
        _create_library_schema(database, version=3)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("ALTER TABLE chapter_analyses RENAME COLUMN analysis_json TO missing")
    elif case == "v0_unrelated":
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE private_unrelated (secret TEXT)")
    else:
        database.touch()
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)

    result, database_check, stdout = _database_check_from_cli(config)

    assert result.exit_code == (1 if expected_status == "fail" else 0)
    assert database_check["status"] == expected_status
    if expected_fragment is not None:
        assert expected_fragment in str(database_check["message"]).lower()
    assert str(database) not in stdout
    assert "private_unrelated" not in stdout
    assert _filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "analysis_table_columns",
    [
        """
        source_system TEXT NOT NULL,
        external_book_id TEXT NOT NULL,
        chapter_index INTEGER NOT NULL CHECK (chapter_index >= 0),
        input_fingerprint TEXT NOT NULL,
        analysis_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
        """,
        """
        source_system TEXT NOT NULL,
        external_book_id TEXT NOT NULL,
        chapter_index INTEGER NOT NULL CHECK (chapter_index >= 0),
        input_fingerprint TEXT,
        analysis_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_system, external_book_id, chapter_index)
        """,
        """
        source_system TEXT NOT NULL,
        external_book_id TEXT NOT NULL,
        chapter_index INTEGER NOT NULL,
        input_fingerprint TEXT NOT NULL,
        analysis_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_system, external_book_id, chapter_index)
        """,
        """
        source_system TEXT NOT NULL,
        external_book_id TEXT NOT NULL,
        chapter_index INTEGER NOT NULL /* CHECK (chapter_index >= 0) */,
        input_fingerprint TEXT NOT NULL,
        analysis_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (source_system, external_book_id, chapter_index)
        """,
        """
        source_system TEXT NOT NULL,
        external_book_id TEXT NOT NULL,
        chapter_index INTEGER NOT NULL,
        input_fingerprint TEXT NOT NULL,
        analysis_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CONSTRAINT "CHECK (chapter_index >= 0)" UNIQUE (input_fingerprint),
        PRIMARY KEY (source_system, external_book_id, chapter_index)
        """,
        """
        source_system TEXT NOT NULL,
        external_book_id TEXT NOT NULL,
        chapter_index INTEGER NOT NULL CHECK (chapter_index >= 0),
        input_fingerprint TEXT NOT NULL,
        analysis_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        generated_extra TEXT GENERATED ALWAYS AS (source_system) VIRTUAL,
        PRIMARY KEY (source_system, external_book_id, chapter_index)
        """,
    ],
    ids=(
        "missing_primary_key",
        "nullable_column",
        "missing_chapter_index_check",
        "comment_check",
        "quoted_check_name",
        "generated_extra",
    ),
)
def test_doctor_rejects_invalid_v3_analysis_cache_shape_without_writing(
    tmp_path: Path,
    analysis_table_columns: str,
) -> None:
    """Accepting a malformed v3 cache table would advertise storage as ready before upsert fails."""
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    _create_library_schema(database, version=3)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TABLE chapter_analyses")
        connection.execute(f"CREATE TABLE chapter_analyses ({analysis_table_columns})")
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)

    result, database_check, stdout = _database_check_from_cli(config)

    assert result.exit_code == 1
    assert database_check["status"] == "fail"
    assert "chapter_analyses" not in stdout
    assert _filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize("view_name", ["private_unrelated", "books"])
def test_doctor_rejects_unversioned_view_schemas_without_writing(
    tmp_path: Path,
    view_name: str,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f'CREATE VIEW "{view_name}" AS SELECT 1 AS private_value')
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)

    result, database_check, stdout = _database_check_from_cli(config)

    assert result.exit_code == 1
    assert database_check["status"] == "fail"
    assert view_name not in stdout
    assert str(database) not in stdout
    assert _filesystem_snapshot(tmp_path) == before


def test_doctor_rejects_v2_schema_composed_of_same_named_views(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        for name in _REQUIRED_VIEW_COLUMNS:
            _create_required_view(connection, name, version=2)
        connection.execute("PRAGMA user_version = 2")
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)

    result, database_check, stdout = _database_check_from_cli(config)

    assert result.exit_code == 1
    assert database_check["status"] == "fail"
    assert str(database) not in stdout
    assert _filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("version", "required_name"),
    [
        (version, required_name)
        for version in (1, 2, 3)
        for required_name in _REQUIRED_VIEW_COLUMNS
        if version == 3 or required_name != "chapter_analyses"
    ],
)
def test_doctor_rejects_required_table_replaced_by_same_named_view(
    tmp_path: Path,
    version: int,
    required_name: str,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    _create_library_schema(database, version=version)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(f'DROP TABLE "{required_name}"')
        _create_required_view(connection, required_name, version=version)
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)

    result, database_check, _stdout = _database_check_from_cli(config)

    assert result.exit_code == 1
    assert database_check["status"] == "fail"
    assert _filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize(
    ("version", "expected_status"),
    [(1, "warn"), (2, "warn"), (3, "pass")],
)
def test_doctor_allows_non_conflicting_extra_schema_objects(
    tmp_path: Path,
    version: int,
    expected_status: str,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    _create_library_schema(database, version=version)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE VIEW diagnostic_view AS SELECT 1 AS value")
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)

    result, database_check, _stdout = _database_check_from_cli(config)

    assert result.exit_code == 0
    assert database_check["status"] == expected_status
    assert _filesystem_snapshot(tmp_path) == before


def test_doctor_rejects_oversized_sparse_database_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    with database.open("wb") as stream:
        stream.truncate(2 * 1024 * 1024 * 1024)
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_metadata_snapshot(tmp_path)
    read_called = False

    def record_forbidden_read(_descriptor: int, _length: int) -> bytes:
        nonlocal read_called
        read_called = True
        return b""

    monkeypatch.setattr(doctor_module.os, "read", record_forbidden_read)

    result, database_check, stdout = _database_check_from_cli(config)

    assert result.exit_code == 1
    assert database_check["status"] == "fail"
    assert read_called is False
    assert str(database) not in stdout
    assert "2147483648" not in stdout
    assert _filesystem_metadata_snapshot(tmp_path) == before


@pytest.mark.parametrize("failure_phase", ["buffer", "deserialize"])
def test_doctor_maps_snapshot_memory_errors_and_closes_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    _create_library_schema(database, version=2)
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)
    real_open = os.open
    real_connect = sqlite3.connect
    opened_descriptors: list[int] = []
    opened_connections: list[MemoryFailingConnection] = []
    private_marker = "private allocation detail"

    class MemoryFailingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection
            self.closed = False

        def deserialize(self, _payload: bytes) -> None:
            raise MemoryError(private_marker)

        def close(self) -> None:
            self.closed = True
            self._connection.close()

    def record_descriptor_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if os.fspath(path) in {os.fspath(data_dir), "library.sqlite3"}:
            opened_descriptors.append(descriptor)
        return descriptor

    def fail_buffer_allocation(_descriptor: int) -> bytes:
        raise MemoryError(private_marker)

    def connect_with_deserialize_failure(
        database_arg: object, *args: Any, **kwargs: Any
    ) -> MemoryFailingConnection:
        connection = MemoryFailingConnection(real_connect(database_arg, *args, **kwargs))
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(doctor_module.os, "open", record_descriptor_open)
    if failure_phase == "buffer":
        monkeypatch.setattr(doctor_module, "_read_descriptor", fail_buffer_allocation)
    else:
        monkeypatch.setattr(
            doctor_module.sqlite3,
            "connect",
            connect_with_deserialize_failure,
        )

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert len(opened_descriptors) == 2
    for descriptor in opened_descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    if failure_phase == "deserialize":
        assert opened_connections and opened_connections[0].closed is True
    else:
        assert not opened_connections
    assert result.exit_code == 1
    assert result.stdout.startswith("{")
    payload = json.loads(result.stdout)
    assert _check(payload, "library_database")["status"] == "fail"
    assert private_marker not in result.stdout
    assert "Traceback" not in result.stdout
    assert _filesystem_snapshot(tmp_path) == before


@pytest.mark.parametrize("kind", ["symlink", "corrupt"])
def test_doctor_rejects_unsafe_or_invalid_existing_database_without_writing(
    tmp_path: Path,
    kind: str,
) -> None:
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    database = data_dir / "library.sqlite3"
    if kind == "symlink":
        target = tmp_path / "outside.sqlite3"
        with closing(sqlite3.connect(target)) as connection, connection:
            connection.execute("CREATE TABLE outside_probe (value TEXT)")
        database.symlink_to(target)
    else:
        database.write_bytes(b"not sqlite and contains /private/details")
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    database_check = _check(payload, "library_database")
    assert database_check["status"] == "fail"
    assert str(database) not in result.stdout
    assert "/private/details" not in result.stdout
    assert _filesystem_snapshot(tmp_path) == before
