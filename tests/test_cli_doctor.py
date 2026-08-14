import json
import sqlite3
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cove_book_forge import doctor as doctor_module
from cove_book_forge.cli import app
from cove_book_forge.doctor import CheckStatus, run_doctor
from cove_book_forge.library import BookLibrary, LibraryDatabase

runner = CliRunner()


def _write_config(path: Path, data_dir: Path, *, enabled: bool) -> Path:
    path.write_text(
        f"""
library:
  enabled: {str(enabled).lower()}
  data_dir: {data_dir}
model:
  provider: test
  model: test
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
        entries.append((relative, stat.S_IFMT(status.st_mode), status.st_size, payload))
    return tuple(entries)


def _check(payload: dict[str, object], name: str) -> dict[str, object]:
    checks = payload["checks"]
    assert isinstance(checks, list)
    return next(check for check in checks if check["name"] == name)


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


def test_doctor_reports_empty_key_environment_as_missing(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setenv("EMPTY_TEST_KEY", "")

    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert _check(payload, "model_api_key") == {
        "name": "model_api_key",
        "status": "fail",
        "message": "Environment variable is missing: EMPTY_TEST_KEY",
    }


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
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE readiness_probe (value TEXT NOT NULL)")
        connection.execute("INSERT INTO readiness_probe VALUES ('unchanged')")
    config = _write_config(tmp_path / "config.yaml", data_dir, enabled=False)
    before = _filesystem_snapshot(tmp_path)

    report = run_doctor(config)

    database_check = next(check for check in report.checks if check.name == "library_database")
    assert database_check.status is CheckStatus.PASS
    assert _filesystem_snapshot(tmp_path) == before
    assert not tuple(data_dir.glob("library.sqlite3-*"))


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
        with sqlite3.connect(target) as connection:
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
