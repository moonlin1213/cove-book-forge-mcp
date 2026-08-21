from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import JsonValue

from cove_book_forge.config import AppConfig
from cove_book_forge.contracts import ChapterSnapshot, ForgeJobStatus
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.forge import WholeBookForge
from cove_book_forge.library import create_book_library
from cove_book_forge.providers import (
    JsonGeneration,
    ProviderCapabilities,
    ProviderUsage,
    TextGeneration,
)


class _Provider:
    def __init__(self, responses: list[dict[str, JsonValue]]) -> None:
        self.responses = deque(responses)
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(json_mode=True)

    @property
    def usage(self) -> ProviderUsage:
        return ProviderUsage()

    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
    ) -> TextGeneration:
        raise AssertionError

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
        json_schema: Mapping[str, JsonValue] | None = None,
    ) -> JsonGeneration:
        del system_prompt, user_prompt, max_output_tokens, temperature, json_schema
        self.calls += 1
        return JsonGeneration(value=self.responses.popleft(), model="fake", usage=ProviderUsage())

    async def healthcheck(self) -> None:
        return None


def _config(tmp_path: Path) -> AppConfig:
    skills = tmp_path / "skills"
    skills.mkdir()
    return AppConfig.model_validate(
        {
            "library": {"enabled": False, "data_dir": tmp_path / "library"},
            "model": {"provider": "fake", "model": "test-model"},
            "outputs": {"skills": {"enabled": True, "canonical_path": skills}},
        }
    )


def _snapshots() -> tuple[ChapterSnapshot, ...]:
    return tuple(
        ChapterSnapshot.model_validate(
            {
                "source_system": "reader",
                "external_book_id": "book",
                "book": {"title": "Whole Book", "total_chapters": 2},
                "chapter": {
                    "index": index,
                    "title": f"Chapter {index + 1}",
                    "content": f"Complete chapter {index + 1} text.",
                },
            }
        )
        for index in range(2)
    )


def _analysis(index: int) -> dict[str, JsonValue]:
    return {"core_idea": f"Idea {index}", "topic_tags": ["whole-book"]}


@pytest.mark.anyio
async def test_external_book_forges_once_and_cached_replan_has_no_calls(tmp_path: Path) -> None:
    config = _config(tmp_path)
    library = create_book_library(config)
    provider = _Provider([_analysis(0), _analysis(1)])
    forge = WholeBookForge(config, library, provider)

    plan = forge.plan_book_to_skill(snapshots=_snapshots())
    assert plan.estimate.model_calls == 2
    accepted = forge.forge_book_to_skill(plan.plan_id, confirmed=True, idempotency_key="once")
    duplicate = forge.forge_book_to_skill(plan.plan_id, confirmed=True, idempotency_key="once")
    assert duplicate.job_id == accepted.job_id
    job = await forge.wait_for_job(accepted.job_id)

    assert job.status is ForgeJobStatus.COMPLETED, job.error
    assert job.processed_chapters == 2
    assert provider.calls == 2
    assert len(tuple((tmp_path / "skills").glob("*/SKILL.md"))) == 1

    zero = _Provider([])
    restarted = WholeBookForge(config, library, zero)
    cached_plan = restarted.plan_book_to_skill(snapshots=_snapshots())
    assert cached_plan.estimate.model_calls == 0
    assert cached_plan.pending_chapters == ()


def test_expired_plan_requires_a_fresh_preflight(tmp_path: Path) -> None:
    config = _config(tmp_path)
    library = create_book_library(config)
    forge = WholeBookForge(config, library, _Provider([]))
    plan = forge.plan_book_to_skill(
        snapshots=_snapshots(), now=datetime.now(UTC) - timedelta(hours=1)
    )

    with pytest.raises(ForgeException) as caught:
        forge.forge_book_to_skill(plan.plan_id, confirmed=True, idempotency_key="expired")

    assert caught.value.code is ForgeErrorCode.PLAN_EXPIRED
