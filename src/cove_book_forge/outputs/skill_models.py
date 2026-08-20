"""Strict internal models for deterministic Agent Skill rendering."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from cove_book_forge.contracts.books import MAX_BOOK_CHAPTERS
from cove_book_forge.path_safety import validate_relative_path

MAX_SKILL_FILES: Final = MAX_BOOK_CHAPTERS + 10
MAX_SUMMARY_ITEMS: Final = 128
MAX_SUMMARY_ITEM_BYTES: Final = 1_000


class AgentSkillModel(BaseModel):
    """Frozen narrow models; untrusted reference text never creates arbitrary fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)


class SkillFileHash(AgentSkillModel):
    path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class AgentSkillChapterManifest(AgentSkillModel):
    index: int = Field(ge=0, lt=MAX_BOOK_CHAPTERS)
    title: str = Field(min_length=1, max_length=120)
    input_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    chapter_path: str = Field(min_length=1, max_length=500)
    core_idea: str = Field(min_length=1, max_length=4000)
    frameworks: tuple[str, ...] = Field(default=(), max_length=MAX_SUMMARY_ITEMS)
    concepts: tuple[str, ...] = Field(default=(), max_length=MAX_SUMMARY_ITEMS)
    mental_models: tuple[str, ...] = Field(default=(), max_length=MAX_SUMMARY_ITEMS)
    methods: tuple[str, ...] = Field(default=(), max_length=MAX_SUMMARY_ITEMS)
    anti_patterns: tuple[str, ...] = Field(default=(), max_length=MAX_SUMMARY_ITEMS)
    decision_rules: tuple[str, ...] = Field(default=(), max_length=MAX_SUMMARY_ITEMS)
    key_takeaways: tuple[str, ...] = Field(default=(), max_length=MAX_SUMMARY_ITEMS)
    topic_tags: tuple[str, ...] = Field(default=(), max_length=MAX_SUMMARY_ITEMS)
    source_locators: tuple[str, ...] = Field(default=(), max_length=MAX_SUMMARY_ITEMS)

    @field_validator("chapter_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator(
        "title",
        "core_idea",
        "frameworks",
        "concepts",
        "mental_models",
        "methods",
        "anti_patterns",
        "decision_rules",
        "key_takeaways",
        "topic_tags",
        "source_locators",
    )
    @classmethod
    def validate_text_budgets(
        cls, value: str | tuple[str, ...], info: ValidationInfo
    ) -> str | tuple[str, ...]:
        values = (value,) if isinstance(value, str) else value
        limit = {
            "title": 120,
            "core_idea": 4_000,
        }.get(info.field_name or "", MAX_SUMMARY_ITEM_BYTES)
        if any(len(item.encode("utf-8")) > limit for item in values):
            raise ValueError("Skill summary text exceeds its byte budget")
        return value


class AgentSkillManifest(AgentSkillModel):
    schema_version: Literal[1] = Field(alias="schema", serialization_alias="schema")
    book_key: str = Field(min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")
    book_title: str = Field(min_length=1, max_length=120)
    author: str = Field(default="", max_length=300)
    skill_slug: str = Field(
        min_length=19,
        max_length=63,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*--[0-9a-f]{16}$",
    )
    total_chapters: int = Field(default=0, ge=0, le=MAX_BOOK_CHAPTERS)
    chapters: tuple[AgentSkillChapterManifest, ...] = Field(
        default=(), max_length=MAX_BOOK_CHAPTERS
    )
    files: tuple[SkillFileHash, ...] = Field(default=(), max_length=MAX_SKILL_FILES)
    generator_version: str = Field(default="cove-book-forge-skill-v1", min_length=1, max_length=120)
    checksum: str = Field(default="", pattern=r"^(|[0-9a-f]{64})$")

    @model_validator(mode="after")
    def require_unique_entries(self) -> AgentSkillManifest:
        if len({chapter.index for chapter in self.chapters}) != len(self.chapters):
            raise ValueError("Skill manifest chapter indices must be unique")
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("Skill manifest file paths must be unique")
        return self

    @field_validator("book_title")
    @classmethod
    def validate_title_budget(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 120:
            raise ValueError("Skill manifest title exceeds its byte budget")
        return value

    @field_validator("author")
    @classmethod
    def validate_author_budget(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 300:
            raise ValueError("Skill manifest author exceeds its byte budget")
        return value


class RenderedAgentSkill(AgentSkillModel):
    files: Mapping[str, bytes]
    manifest: AgentSkillManifest
    skill_slug: str = Field(
        min_length=19,
        max_length=63,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*--[0-9a-f]{16}$",
    )
    chapter_path: str = Field(min_length=1, max_length=500)

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: Mapping[str, bytes]) -> Mapping[str, bytes]:
        for path, payload in value.items():
            validate_relative_path(path)
            if not isinstance(payload, bytes):
                raise ValueError("rendered files must be bytes")
        return MappingProxyType(dict(value))

    @field_validator("chapter_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)
