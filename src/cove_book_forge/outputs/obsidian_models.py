from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cove_book_forge.path_safety import validate_relative_path


class ObsidianModel(BaseModel):
    """Strict immutable internal data; no arbitrary manifest fields are retained."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)


class ObsidianCardManifest(ObsidianModel):
    stable_id: str = Field(min_length=16, max_length=64, pattern=r"^[0-9a-f]+$")
    kind: Literal["concept", "decision_rule"]
    title: str = Field(min_length=1, max_length=120)
    path: str = Field(min_length=1, max_length=500)
    chapter_index: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_relative_path(value)


class ObsidianChapterManifest(ObsidianModel):
    index: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=120)
    input_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    note_path: str = Field(min_length=1, max_length=500)
    card_paths: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()

    @field_validator("note_path")
    @classmethod
    def validate_note_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("card_paths")
    @classmethod
    def validate_card_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(validate_relative_path(path) for path in value)


class ObsidianBookManifest(ObsidianModel):
    schema_version: Literal[1] = Field(alias="schema", serialization_alias="schema")
    book_key: str = Field(min_length=16, max_length=16, pattern=r"^[0-9a-f]{16}$")
    book_title: str = Field(min_length=1, max_length=120)
    book_directory: str = Field(min_length=1, max_length=240)
    moc_path: str = Field(min_length=1, max_length=500)
    total_chapters: int = Field(default=0, ge=0)
    chapters: tuple[ObsidianChapterManifest, ...] = ()
    cards: tuple[ObsidianCardManifest, ...] = ()
    checksum: str = Field(default="", pattern=r"^(|[0-9a-f]{64})$")

    @field_validator("book_directory", "moc_path")
    @classmethod
    def validate_moc_path(cls, value: str) -> str:
        return validate_relative_path(value)


class RenderedObsidianBook(ObsidianModel):
    files: Mapping[str, bytes]
    manifest: ObsidianBookManifest
    chapter_path: str
    moc_path: str
    card_paths: tuple[str, ...]

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: Mapping[str, bytes]) -> Mapping[str, bytes]:
        for path, payload in value.items():
            validate_relative_path(path)
            if not isinstance(payload, bytes):
                raise ValueError("rendered files must be bytes")
        return MappingProxyType(dict(value))

    @field_validator("chapter_path", "moc_path", "card_paths")
    @classmethod
    def validate_rendered_paths(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        if isinstance(value, tuple):
            return tuple(validate_relative_path(path) for path in value)
        return validate_relative_path(value)
