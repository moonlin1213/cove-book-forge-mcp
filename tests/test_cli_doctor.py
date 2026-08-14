import json
from pathlib import Path

from typer.testing import CliRunner

from cove_book_forge.cli import app

runner = CliRunner()


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
    assert payload["checks"][1]["name"] == "library_data"
    assert payload["checks"][1]["status"] == "fail"
