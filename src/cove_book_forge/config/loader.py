from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]
from platformdirs import user_config_path, user_data_path
from pydantic import ValidationError

from cove_book_forge.config.models import AppConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException


def default_config_path() -> Path:
    return user_config_path("cove-book-forge-mcp") / "config.yaml"


def default_data_path() -> Path:
    return user_data_path("cove-book-forge-mcp")


def library_data_path(config: AppConfig) -> Path:
    return config.library.data_dir or default_data_path()


def load_config(path: Path | None = None) -> AppConfig:
    source = (path or default_config_path()).expanduser()
    try:
        raw: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("configuration root must be a mapping")
        return AppConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise ForgeException(
            ForgeErrorCode.CONFIG_INVALID,
            "Configuration is invalid.",
            details={"path": str(source)},
            cause=exc,
        ) from exc


def dump_config(config: AppConfig) -> str:
    payload = config.model_dump(mode="json")
    return cast(str, yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
