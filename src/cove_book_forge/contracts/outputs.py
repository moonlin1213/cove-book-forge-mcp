from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from cove_book_forge.contracts.base import ContractModel
from cove_book_forge.path_safety import validate_relative_path


class ObsidianPublishResult(ContractModel):
    """Public result returned after a managed Obsidian publication."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    book_key: str = Field(min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")
    chapter_path: str = Field(min_length=1, max_length=500)
    moc_path: str = Field(min_length=1, max_length=500)
    card_paths: tuple[str, ...] = ()
    input_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    changed_paths: tuple[str, ...] = ()
    unchanged: bool = False

    @field_validator("chapter_path", "moc_path", "card_paths", "changed_paths")
    @classmethod
    def validate_paths(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        if isinstance(value, tuple):
            return tuple(validate_relative_path(path) for path in value)
        return validate_relative_path(value)


class SkillInstallResult(ContractModel):
    """A safe relative display receipt for one Agent Skill installation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target: Literal["agents", "codex", "claude"]
    path: str = Field(min_length=1, max_length=500)
    strategy: Literal["symlink", "copy"]
    unchanged: bool = False

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class SkillPublishResult(ContractModel):
    """Public result returned after a managed Agent Skill publication."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    book_key: str = Field(min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")
    skill_slug: str = Field(
        min_length=19,
        max_length=63,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*--[0-9a-f]{16}$",
    )
    canonical_path: str = Field(min_length=1, max_length=500)
    chapter_path: str = Field(min_length=1, max_length=500)
    input_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    changed_paths: tuple[str, ...] = ()
    installations: tuple[SkillInstallResult, ...] = ()
    unchanged: bool = False

    @field_validator("canonical_path", "chapter_path", "changed_paths")
    @classmethod
    def validate_paths(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        if isinstance(value, tuple):
            return tuple(validate_relative_path(path) for path in value)
        return validate_relative_path(value)
