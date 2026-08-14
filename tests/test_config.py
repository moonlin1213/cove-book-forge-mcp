from pathlib import Path

import pytest

from cove_book_forge.config.loader import dump_config, load_config
from cove_book_forge.config.models import AppConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException


def test_config_round_trip_stores_key_name_not_secret(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
library:
  enabled: false
model:
  provider: openai-compatible
  base_url: https://api.deepseek.com
  model: deepseek-v4-flash
  api_key_env: DEEPSEEK_API_KEY
outputs:
  obsidian:
    enabled: false
  skills:
    enabled: false
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "actual-secret")
    config = load_config(path)
    rendered = dump_config(config)
    assert config.model.api_key_env == "DEEPSEEK_API_KEY"
    assert "DEEPSEEK_API_KEY" in rendered
    assert "actual-secret" not in rendered


def test_invalid_config_is_a_structured_public_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("model: {}", encoding="utf-8")
    with pytest.raises(ForgeException) as caught:
        load_config(path)
    assert caught.value.code is ForgeErrorCode.CONFIG_INVALID


def test_defaults_are_local_first_and_require_full_book_confirmation() -> None:
    config = AppConfig.model_validate(
        {"model": {"provider": "openai-compatible", "model": "local-model"}}
    )
    assert config.library.enabled is True
    assert config.full_book_forge.require_preflight_confirmation is True
    assert config.telemetry_enabled is False
