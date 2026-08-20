from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator

from cove_book_forge.path_safety import validate_relative_path


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
    json_mode: bool | None = None
    max_concurrency: int = Field(default=2, ge=1, le=16)
    requests_per_minute: int = Field(default=20, ge=1, le=10_000)
    request_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    default_max_output_tokens: int = Field(default=4_096, ge=1, le=131_072)


class AnalysisConfig(ConfigModel):
    prompt_template_version: str = Field(
        default="chapter-analysis-v1", min_length=1, max_length=120
    )
    generator_version: str = Field(default="cove-analysis-v1", min_length=1, max_length=120)
    max_chunk_characters: int = Field(default=24_000, ge=128, le=1_000_000)
    include_provider_in_fingerprint: bool = False


class ObsidianOutputConfig(ConfigModel):
    enabled: bool = False
    vault_path: Path | None = None
    notes_folder: str = Field(default="Books", min_length=1, max_length=120)
    cards_folder: str = Field(default="Cards", min_length=1, max_length=120)

    @field_validator("notes_folder", "cards_folder", mode="before")
    @classmethod
    def normalize_safe_relative_folder(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("Obsidian folders must be strings")
        return validate_relative_path(value, max_path_bytes=120)

    @model_validator(mode="after")
    def require_absolute_enabled_path(self) -> Self:
        if self.enabled and (self.vault_path is None or not self.vault_path.is_absolute()):
            raise ValueError("enabled Obsidian output requires an absolute vault_path")
        return self


class SkillOutputConfig(ConfigModel):
    enabled: bool = False
    canonical_path: Path | None = None
    install_to: tuple[Literal["agents", "codex", "claude"], ...] = ()

    @field_validator("install_to")
    @classmethod
    def reject_duplicate_install_targets(
        cls, value: tuple[Literal["agents", "codex", "claude"], ...]
    ) -> tuple[Literal["agents", "codex", "claude"], ...]:
        if len(value) != len(set(value)):
            raise ValueError("Skill install_to targets must be unique")
        return value

    @model_validator(mode="after")
    def require_absolute_enabled_path(self) -> Self:
        if self.enabled and (self.canonical_path is None or not self.canonical_path.is_absolute()):
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
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    outputs: OutputsConfig = Field(default_factory=OutputsConfig)
    full_book_forge: FullBookForgeConfig = Field(default_factory=FullBookForgeConfig)
    telemetry_enabled: Literal[False] = False
