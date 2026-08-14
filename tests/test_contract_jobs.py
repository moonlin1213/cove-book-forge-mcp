from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cove_book_forge.contracts.jobs import (
    CostEstimate,
    ForgeJob,
    ForgeJobStatus,
    ForgePlan,
    ForgeTarget,
)


def test_forge_plan_records_preflight_scope_and_expiry() -> None:
    now = datetime.now(UTC)
    plan = ForgePlan(
        plan_id="plan-1",
        book_id="book-1",
        book_fingerprint="sha256:abc",
        target=ForgeTarget.SKILL,
        total_chapters=12,
        processed_chapters=3,
        pending_chapters=(3, 4, 5, 6, 7, 8, 9, 10, 11),
        provider="openai-compatible",
        model="deepseek-v4-flash",
        generator_version="0.1",
        prompt_version="chapter-v1",
        estimate=CostEstimate(input_tokens=380_000, model_calls=12),
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    assert plan.remaining_chapters == 9
    assert plan.is_expired(now) is False

    invalid_scope = plan.model_dump()
    invalid_scope["processed_chapters"] = 13
    with pytest.raises(ValidationError):
        ForgePlan.model_validate(invalid_scope)


def test_cost_estimate_requires_a_complete_ordered_money_range() -> None:
    with pytest.raises(ValidationError):
        CostEstimate(currency="USD", minimum=Decimal("1.00"))
    with pytest.raises(ValidationError):
        CostEstimate(
            currency="USD",
            minimum=Decimal("2.00"),
            maximum=Decimal("1.00"),
        )


def test_job_exposes_progress_without_source_text() -> None:
    job = ForgeJob(
        job_id="job-1",
        book_id="book-1",
        target=ForgeTarget.SKILL,
        status=ForgeJobStatus.ANALYZING,
        processed_chapters=4,
        total_chapters=12,
    )
    assert job.progress == 4 / 12
    assert "content" not in job.model_dump()

    with pytest.raises(ValidationError):
        ForgeJob(
            job_id="job-invalid",
            book_id="book-1",
            target=ForgeTarget.SKILL,
            processed_chapters=13,
            total_chapters=12,
        )
