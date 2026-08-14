from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LibraryConfig(ConfigModel):
    enabled: bool = True
    copy_imports: bool = True
    data_dir: Path | None = None

    @model_validator(mode="after")
    def require_absolute_custom_data_path(self) -> Self:
        if self.data_dir is not None and not self.data_dir.is_absolute():
            raise ValueError("custom library data_dir must be absolute")
        return self


class ModelConfig(ConfigModel):
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=240)
    base_url: HttpUrl | None = None
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    max_concurrency: int = Field(default=2, ge=1, le=16)
    requests_per_minute: int = Field(default=20, ge=1, le=10_000)
    request_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    default_max_output_tokens: int = Field(default=4_096, ge=1, le=131_072)


class ObsidianOutputConfig(ConfigModel):
    enabled: bool = False
    vault_path: Path | None = None
    notes_folder: str = Field(default="Books", min_length=1, max_length=120)
    cards_folder: str = Field(default="Cards", min_length=1, max_length=120)

    @model_validator(mode="after")
    def require_absolute_enabled_path(self) -> Self:
        if self.enabled and (self.vault_path is None or not self.vault_path.is_absolute()):
            raise ValueError("enabled Obsidian output requires an absolute vault_path")
        return self


class SkillOutputConfig(ConfigModel):
    enabled: bool = False
    canonical_path: Path | None = None
    install_to: tuple[Literal["agents", "codex", "claude"], ...] = ()

    @model_validator(mode="after")
    def require_absolute_enabled_path(self) -> Self:
        if self.enabled and (
            self.canonical_path is None or not self.canonical_path.is_absolute()
        ):
            raise ValueError("enabled Skill output requires an absolute canonical_path")
        return self


class OutputsConfig(ConfigModel):
    obsidian: ObsidianOutputConfig = Field(default_factory=ObsidianOutputConfig)
    skills: SkillOutputConfig = Field(default_factory=SkillOutputConfig)


class FullBookForgeConfig(ConfigModel):
    require_preflight_confirmation: bool = True
    plan_ttl_minutes: int = Field(default=30, ge=1, le=1440)


class AppConfig(ConfigModel):
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    model: ModelConfig
    outputs: OutputsConfig = Field(default_factory=OutputsConfig)
    full_book_forge: FullBookForgeConfig = Field(default_factory=FullBookForgeConfig)
    telemetry_enabled: Literal[False] = False
