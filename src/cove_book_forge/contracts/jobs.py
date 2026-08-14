from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from cove_book_forge.contracts.base import ContractModel
from cove_book_forge.errors import ForgeErrorDetail


class ForgeTarget(StrEnum):
    OBSIDIAN = "obsidian"
    SKILL = "skill"


class ForgeJobStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    PARSING = "parsing"
    ANALYZING = "analyzing"
    SYNTHESIZING = "synthesizing"
    VALIDATING = "validating"
    PUBLISHING = "publishing"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ForgeJobControl(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


class CostEstimate(ContractModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    minimum: Decimal | None = Field(default=None, ge=0)
    maximum: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_complete_ordered_money_range(self) -> Self:
        values = (self.currency, self.minimum, self.maximum)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("currency, minimum, and maximum must be supplied together")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self

    @property
    def available(self) -> bool:
        return self.currency is not None and self.minimum is not None and self.maximum is not None


class ForgePlan(ContractModel):
    plan_id: str = Field(min_length=1, max_length=120)
    book_id: str = Field(min_length=1, max_length=120)
    book_fingerprint: str = Field(min_length=1, max_length=160)
    target: ForgeTarget
    total_chapters: int = Field(ge=0)
    processed_chapters: int = Field(ge=0)
    pending_chapters: tuple[int, ...] = ()
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=240)
    generator_version: str = Field(min_length=1, max_length=80)
    prompt_version: str = Field(min_length=1, max_length=80)
    estimate: CostEstimate
    created_at: datetime
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value

    @model_validator(mode="after")
    def require_consistent_scope(self) -> Self:
        if self.processed_chapters > self.total_chapters:
            raise ValueError("processed_chapters must not exceed total_chapters")
        if len(set(self.pending_chapters)) != len(self.pending_chapters):
            raise ValueError("pending_chapters must not contain duplicates")
        if any(index < 0 or index >= self.total_chapters for index in self.pending_chapters):
            raise ValueError("pending chapter index is outside the book")
        if self.processed_chapters + len(self.pending_chapters) != self.total_chapters:
            raise ValueError("processed and pending chapters must cover the book")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self

    @property
    def remaining_chapters(self) -> int:
        return len(self.pending_chapters)

    def is_expired(self, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return now >= self.expires_at


def _now_utc() -> datetime:
    return datetime.now(UTC)


class ForgeJob(ContractModel):
    job_id: str = Field(min_length=1, max_length=120)
    book_id: str = Field(min_length=1, max_length=120)
    target: ForgeTarget
    status: ForgeJobStatus = ForgeJobStatus.QUEUED
    processed_chapters: int = Field(default=0, ge=0)
    total_chapters: int = Field(default=0, ge=0)
    current_chapter: int | None = Field(default=None, ge=0)
    failed_chapters: tuple[int, ...] = ()
    error: ForgeErrorDetail | None = None
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value

    @model_validator(mode="after")
    def require_consistent_progress(self) -> Self:
        if self.processed_chapters > self.total_chapters:
            raise ValueError("processed_chapters must not exceed total_chapters")
        indexes = self.failed_chapters
        if self.current_chapter is not None:
            indexes = (*indexes, self.current_chapter)
        if any(index < 0 or index >= self.total_chapters for index in indexes):
            raise ValueError("job chapter index is outside the book")
        if len(set(self.failed_chapters)) != len(self.failed_chapters):
            raise ValueError("failed_chapters must not contain duplicates")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        return self

    @property
    def progress(self) -> float:
        if self.total_chapters == 0:
            return 0.0
        return min(1.0, max(0.0, self.processed_chapters / self.total_chapters))


class ForgeAccepted(ContractModel):
    accepted: Literal[True] = True
    job_id: str = Field(min_length=1, max_length=120)
    status: ForgeJobStatus
    target: ForgeTarget
