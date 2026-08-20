from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue

from cove_book_forge.analysis import ChapterAnalysisCache, ChapterAnalyzer
from cove_book_forge.config import AnalysisConfig, AppConfig, ModelConfig
from cove_book_forge.contracts import ChapterAnalysis, ChapterSnapshot
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.library import BookLibrary
from cove_book_forge.providers import (
    JsonGeneration,
    ProviderCapabilities,
    ProviderUsage,
    TextGeneration,
)


def _snapshot(*, content: str = "A clear chapter body.") -> ChapterSnapshot:
    return ChapterSnapshot.model_validate(
        {
            "source_system": "external",
            "external_book_id": "book-1",
            "book": {"title": "Private Book"},
            "chapter": {"index": 2, "title": "Chapter Two", "content": content},
            "user_notes": [{"id": "note-1", "text": "Private note"}],
        }
    )


def _model(**changes: object) -> ModelConfig:
    return ModelConfig.model_validate({"provider": "local", "model": "chapter-model", **changes})


def _valid_value(*, core_idea: str = "The central idea.") -> dict[str, JsonValue]:
    return {"core_idea": core_idea}


class FakeCache:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str, int, str], ChapterAnalysis] = {}
        self.store_calls = 0

    def load_chapter_analysis(
        self,
        source_system: str,
        external_book_id: str,
        chapter_index: int,
        input_fingerprint: str,
    ) -> ChapterAnalysis | None:
        return self.entries.get((source_system, external_book_id, chapter_index, input_fingerprint))

    def store_chapter_analysis(
        self,
        source_system: str,
        external_book_id: str,
        chapter_index: int,
        input_fingerprint: str,
        analysis: ChapterAnalysis,
    ) -> None:
        self.store_calls += 1
        self.entries[(source_system, external_book_id, chapter_index, input_fingerprint)] = analysis


class RecheckingCache(FakeCache):
    def __init__(self, loads: list[ChapterAnalysis | None]) -> None:
        super().__init__()
        self._loads: deque[ChapterAnalysis | None] = deque(loads)

    def load_chapter_analysis(
        self,
        source_system: str,
        external_book_id: str,
        chapter_index: int,
        input_fingerprint: str,
    ) -> ChapterAnalysis | None:
        del source_system, external_book_id, chapter_index, input_fingerprint
        return self._loads.popleft() if self._loads else None


class FakeProvider:
    def __init__(self, responses: list[dict[str, JsonValue] | BaseException]) -> None:
        self._responses: deque[dict[str, JsonValue] | BaseException] = deque(responses)
        self.calls: list[tuple[str, str, int, Mapping[str, JsonValue] | None]] = []
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

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
        raise AssertionError("ChapterAnalyzer must use generate_json")

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
        json_schema: Mapping[str, JsonValue] | None = None,
    ) -> JsonGeneration:
        del temperature
        self.calls.append((system_prompt, user_prompt, max_output_tokens, json_schema))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        response = self._responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return JsonGeneration(value=response, model="fake", usage=ProviderUsage())

    async def healthcheck(self) -> None:
        return None


def _analyzer(
    provider: FakeProvider,
    cache: FakeCache,
    *,
    analysis_config: AnalysisConfig | None = None,
    model_config: ModelConfig | None = None,
) -> ChapterAnalyzer:
    return ChapterAnalyzer(
        provider,
        cache,
        analysis_config or AnalysisConfig(),
        model_config or _model(),
    )


def test_book_library_satisfies_runtime_chapter_analysis_cache_protocol(tmp_path: Path) -> None:
    """Removing either cache method breaks dependency injection at the analyzer boundary."""
    library = BookLibrary(
        AppConfig.model_validate(
            {
                "library": {"enabled": False, "data_dir": tmp_path / "library"},
                "model": {"provider": "local", "model": "chapter-model"},
            }
        )
    )

    assert isinstance(library, ChapterAnalysisCache)


@pytest.mark.anyio
async def test_analyze_generates_strictly_valid_analysis_with_schema_and_configured_limit() -> None:
    provider = FakeProvider([_valid_value()])
    cache = FakeCache()

    result = await _analyzer(provider, cache).analyze(_snapshot())

    assert result.analysis == ChapterAnalysis(core_idea="The central idea.")
    assert result.cache_hit is False
    assert len(result.input_fingerprint) == 64
    assert cache.store_calls == 1
    assert len(provider.calls) == 1
    system_prompt, user_prompt, max_tokens, schema = provider.calls[0]
    assert max_tokens == 4_096
    assert schema == ChapterAnalysis.model_json_schema()
    assert "Private Book" not in system_prompt
    assert "Private note" in user_prompt


@pytest.mark.anyio
async def test_analyze_matching_cache_hit_makes_zero_provider_calls() -> None:
    provider = FakeProvider([_valid_value()])
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)
    snapshot = _snapshot()
    generated = await analyzer.analyze(snapshot)

    reused = await analyzer.analyze(snapshot)

    assert reused.analysis == generated.analysis
    assert reused.input_fingerprint == generated.input_fingerprint
    assert reused.cache_hit is True
    assert len(provider.calls) == 1
    assert cache.store_calls == 1


@pytest.mark.anyio
async def test_analyze_rechecks_cache_inside_non_force_attempt_before_provider_generation() -> None:
    cached = ChapterAnalysis(core_idea="Written by another analyzer.")
    provider = FakeProvider([_valid_value(core_idea="Provider should not run")])
    cache = RecheckingCache([None, cached])

    result = await _analyzer(provider, cache).analyze(_snapshot())

    assert result.analysis == cached
    assert result.cache_hit is True
    assert len(provider.calls) == 0
    assert cache.store_calls == 0


@pytest.mark.anyio
async def test_analyze_force_refreshes_after_joining_non_force_cache_recheck_outcome() -> None:
    cached = ChapterAnalysis(core_idea="Written by another analyzer.")
    provider = FakeProvider([_valid_value(core_idea="Forced refresh.")])
    provider.release = asyncio.Event()
    cache = RecheckingCache([None, cached])
    analyzer = _analyzer(provider, cache)

    non_force = asyncio.create_task(analyzer.analyze(_snapshot()))
    forced = asyncio.create_task(analyzer.analyze(_snapshot(), force=True))
    await provider.started.wait()
    assert len(provider.calls) == 1
    provider.release.set()
    non_force_result, forced_result = await asyncio.gather(non_force, forced)

    assert non_force_result.analysis == cached
    assert non_force_result.cache_hit is True
    assert forced_result.analysis == ChapterAnalysis(core_idea="Forced refresh.")
    assert forced_result.cache_hit is False
    assert len(provider.calls) == 1
    assert cache.store_calls == 1


@pytest.mark.anyio
async def test_analyze_changed_input_or_analysis_config_misses_cache() -> None:
    provider = FakeProvider(
        [
            _valid_value(core_idea="first"),
            _valid_value(core_idea="second"),
            _valid_value(core_idea="changed config"),
        ]
    )
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)
    await analyzer.analyze(_snapshot())

    changed_input = await analyzer.analyze(_snapshot(content="Changed body."))
    changed_config = await _analyzer(
        provider,
        cache,
        analysis_config=AnalysisConfig(prompt_template_version="chapter-analysis-v2"),
    ).analyze(_snapshot(content="Changed body."))

    assert changed_input.cache_hit is False
    assert changed_config.cache_hit is False
    assert len(provider.calls) == 3


@pytest.mark.anyio
async def test_analyze_schema_change_misses_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeProvider(
        [_valid_value(core_idea="first"), _valid_value(core_idea="schema changed")]
    )
    cache = FakeCache()
    snapshot = _snapshot()
    await _analyzer(provider, cache).analyze(snapshot)
    original = ChapterAnalysis.model_json_schema

    def changed_schema() -> dict[str, object]:
        return {**original(), "title": "Changed schema"}

    monkeypatch.setattr(ChapterAnalysis, "model_json_schema", changed_schema)
    result = await _analyzer(provider, cache).analyze(snapshot)

    assert result.cache_hit is False
    assert result.analysis.core_idea == "schema changed"
    assert len(provider.calls) == 2


@pytest.mark.anyio
async def test_analyze_provider_change_hits_by_default_and_misses_when_opted_in() -> None:
    cache = FakeCache()
    provider = FakeProvider(
        [
            _valid_value(core_idea="default"),
            _valid_value(core_idea="first opted"),
            _valid_value(core_idea="changed opted"),
        ]
    )
    snapshot = _snapshot()
    await _analyzer(provider, cache, model_config=_model(model="first")).analyze(snapshot)

    default_reuse = await _analyzer(provider, cache, model_config=_model(model="second")).analyze(
        snapshot
    )
    opted_first = await _analyzer(
        provider,
        cache,
        analysis_config=AnalysisConfig(include_provider_in_fingerprint=True),
        model_config=_model(model="first"),
    ).analyze(snapshot)
    opted_changed = await _analyzer(
        provider,
        cache,
        analysis_config=AnalysisConfig(include_provider_in_fingerprint=True),
        model_config=_model(model="second"),
    ).analyze(snapshot)

    assert default_reuse.cache_hit is True
    assert opted_first.cache_hit is False
    assert opted_changed.cache_hit is False
    assert len(provider.calls) == 3


@pytest.mark.anyio
async def test_analyze_force_bypasses_cache_and_overwrites_only_after_success() -> None:
    provider = FakeProvider([_valid_value(core_idea="first"), _valid_value(core_idea="forced")])
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)
    snapshot = _snapshot()
    first = await analyzer.analyze(snapshot)

    forced = await analyzer.analyze(snapshot, force=True)
    reused = await analyzer.analyze(snapshot)

    assert first.analysis.core_idea == "first"
    assert forced.analysis.core_idea == "forced"
    assert forced.cache_hit is False
    assert reused.analysis == forced.analysis
    assert reused.cache_hit is True
    assert len(provider.calls) == 2
    assert cache.store_calls == 2


@pytest.mark.anyio
async def test_analyze_failed_force_leaves_last_valid_cache_entry_intact() -> None:
    provider = FakeProvider([_valid_value(core_idea="saved"), {}, {}])
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)
    snapshot = _snapshot()
    await analyzer.analyze(snapshot)

    with pytest.raises(ForgeException) as raised:
        await analyzer.analyze(snapshot, force=True)
    reused = await analyzer.analyze(snapshot)

    assert raised.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert reused.cache_hit is True
    assert reused.analysis.core_idea == "saved"
    assert len(provider.calls) == 3
    assert cache.store_calls == 1


@pytest.mark.anyio
async def test_analyze_invalid_schema_regenerates_once_and_only_caches_final_result() -> None:
    provider = FakeProvider([{}, _valid_value(core_idea="repaired")])
    cache = FakeCache()

    result = await _analyzer(provider, cache).analyze(_snapshot())

    assert result.analysis.core_idea == "repaired"
    assert len(provider.calls) == 2
    assert cache.store_calls == 1


@pytest.mark.anyio
async def test_analyze_provider_invalid_output_regenerates_once() -> None:
    provider = FakeProvider(
        [
            ForgeException(ForgeErrorCode.MODEL_OUTPUT_INVALID, "raw provider failure"),
            _valid_value(),
        ]
    )
    cache = FakeCache()

    result = await _analyzer(provider, cache).analyze(_snapshot())

    assert result.cache_hit is False
    assert len(provider.calls) == 2
    assert cache.store_calls == 1


@pytest.mark.anyio
async def test_analyze_second_provider_invalid_output_returns_fixed_public_error() -> None:
    provider = FakeProvider(
        [
            ForgeException(ForgeErrorCode.MODEL_OUTPUT_INVALID, "first private output"),
            ForgeException(ForgeErrorCode.MODEL_OUTPUT_INVALID, "second private output"),
        ]
    )
    cache = FakeCache()

    with pytest.raises(ForgeException) as raised:
        await _analyzer(provider, cache).analyze(_snapshot(content="PRIVATE CHAPTER"))

    assert raised.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert str(raised.value) == "Model provider returned invalid output."
    assert "private" not in str(raised.value)
    assert len(provider.calls) == 2
    assert cache.store_calls == 0


@pytest.mark.anyio
async def test_analyze_second_invalid_result_returns_closed_public_error_without_cache_or_content() -> (
    None
):
    private_content = "DO_NOT_LEAK_PRIVATE_SOURCE"
    provider = FakeProvider([{}, {"core_idea": 3}])
    cache = FakeCache()

    with pytest.raises(ForgeException) as raised:
        await _analyzer(provider, cache).analyze(_snapshot(content=private_content))

    assert raised.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert str(raised.value) == "Model provider returned invalid output."
    assert private_content not in str(raised.value)
    assert "core_idea" not in str(raised.value)
    assert len(provider.calls) == 2
    assert cache.store_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "code",
    [
        ForgeErrorCode.MODEL_AUTH_FAILED,
        ForgeErrorCode.MODEL_RATE_LIMITED,
        ForgeErrorCode.MODEL_UNAVAILABLE,
        ForgeErrorCode.CONFIG_INVALID,
    ],
)
async def test_analyze_non_output_provider_errors_propagate_without_repair(
    code: ForgeErrorCode,
) -> None:
    provider = FakeProvider([ForgeException(code, "sensitive upstream detail")])
    cache = FakeCache()

    with pytest.raises(ForgeException) as raised:
        await _analyzer(provider, cache).analyze(_snapshot())

    assert raised.value.code is code
    assert len(provider.calls) == 1
    assert cache.store_calls == 0


@pytest.mark.anyio
@pytest.mark.parametrize("exception_type", [ValueError, RuntimeError])
async def test_analyze_sanitizes_unexpected_provider_exceptions_without_repair(
    exception_type: type[Exception],
) -> None:
    private_error = "RAW_MODEL prompt=PRIVATE_PROMPT secret=PRIVATE_SECRET"
    provider = FakeProvider([exception_type(private_error)])
    cache = FakeCache()

    with pytest.raises(ForgeException) as raised:
        await _analyzer(provider, cache).analyze(_snapshot(content="PRIVATE SOURCE"))

    assert raised.value.code is ForgeErrorCode.MODEL_UNAVAILABLE
    assert str(raised.value) == "Model provider is unavailable."
    assert raised.value.details == {}
    assert private_error not in str(raised.value)
    assert private_error not in str(raised.value.as_detail().model_dump(mode="json"))
    assert len(provider.calls) == 1
    assert cache.store_calls == 0


@pytest.mark.anyio
async def test_analyze_same_key_concurrently_shares_one_generation() -> None:
    provider = FakeProvider([_valid_value()])
    provider.release = asyncio.Event()
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)
    snapshot = _snapshot()

    first = asyncio.create_task(analyzer.analyze(snapshot))
    await provider.started.wait()
    second = asyncio.create_task(analyzer.analyze(snapshot))
    await asyncio.sleep(0)
    assert len(provider.calls) == 1
    provider.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.analysis == second_result.analysis
    assert first_result.cache_hit is False
    assert second_result.cache_hit is True
    assert len(provider.calls) == 1
    assert cache.store_calls == 1


@pytest.mark.anyio
async def test_analyze_concurrent_force_calls_share_one_fresh_generation() -> None:
    provider = FakeProvider([_valid_value(core_idea="fresh")])
    provider.release = asyncio.Event()
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)
    snapshot = _snapshot()

    first = asyncio.create_task(analyzer.analyze(snapshot, force=True))
    await provider.started.wait()
    second = asyncio.create_task(analyzer.analyze(snapshot, force=True))
    await asyncio.sleep(0)
    assert len(provider.calls) == 1
    provider.release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.analysis == second_result.analysis
    assert first_result.cache_hit is False
    assert second_result.cache_hit is True
    assert len(provider.calls) == 1
    assert cache.store_calls == 1


@pytest.mark.anyio
async def test_analyze_failure_removes_idle_singleflight_lock_and_later_call_retries() -> None:
    provider = FakeProvider(
        [ForgeException(ForgeErrorCode.MODEL_UNAVAILABLE, "temporary"), _valid_value()]
    )
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)

    with pytest.raises(ForgeException, match="Model provider is unavailable"):
        await analyzer.analyze(_snapshot())
    retried = await analyzer.analyze(_snapshot())

    assert retried.analysis.core_idea == "The central idea."
    assert len(provider.calls) == 2
    assert cache.store_calls == 1


@pytest.mark.anyio
async def test_analyze_non_force_failure_cohort_shares_one_closed_outcome() -> None:
    provider = FakeProvider([ForgeException(ForgeErrorCode.MODEL_UNAVAILABLE, "private failure")])
    provider.release = asyncio.Event()
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)

    tasks = [asyncio.create_task(analyzer.analyze(_snapshot())) for _ in range(3)]
    await provider.started.wait()
    await asyncio.sleep(0)
    assert len(provider.calls) == 1
    provider.release.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    assert len(provider.calls) == 1
    assert cache.store_calls == 0
    assert all(
        isinstance(outcome, ForgeException)
        and outcome.code is ForgeErrorCode.MODEL_UNAVAILABLE
        and str(outcome) == "Model provider is unavailable."
        for outcome in outcomes
    )


@pytest.mark.anyio
async def test_analyze_force_failure_cohort_shares_one_closed_outcome() -> None:
    provider = FakeProvider([ForgeException(ForgeErrorCode.MODEL_UNAVAILABLE, "private failure")])
    provider.release = asyncio.Event()
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)

    tasks = [asyncio.create_task(analyzer.analyze(_snapshot(), force=True)) for _ in range(3)]
    await provider.started.wait()
    await asyncio.sleep(0)
    assert len(provider.calls) == 1
    provider.release.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    assert len(provider.calls) == 1
    assert cache.store_calls == 0
    assert all(
        isinstance(outcome, ForgeException) and outcome.code is ForgeErrorCode.MODEL_UNAVAILABLE
        for outcome in outcomes
    )


@pytest.mark.anyio
async def test_analyze_mixed_force_failure_cohort_shares_one_closed_outcome() -> None:
    provider = FakeProvider([ForgeException(ForgeErrorCode.MODEL_UNAVAILABLE, "private failure")])
    provider.release = asyncio.Event()
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)

    tasks = [
        asyncio.create_task(analyzer.analyze(_snapshot(), force=True)),
        asyncio.create_task(analyzer.analyze(_snapshot())),
        asyncio.create_task(analyzer.analyze(_snapshot(), force=True)),
    ]
    await provider.started.wait()
    await asyncio.sleep(0)
    assert len(provider.calls) == 1
    provider.release.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    assert len(provider.calls) == 1
    assert cache.store_calls == 0
    assert all(
        isinstance(outcome, ForgeException) and outcome.code is ForgeErrorCode.MODEL_UNAVAILABLE
        for outcome in outcomes
    )


@pytest.mark.anyio
async def test_analyze_failure_cohort_can_retry_after_all_waiters_exit() -> None:
    provider = FakeProvider(
        [ForgeException(ForgeErrorCode.MODEL_UNAVAILABLE, "private failure"), _valid_value()]
    )
    provider.release = asyncio.Event()
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)

    tasks = [asyncio.create_task(analyzer.analyze(_snapshot())) for _ in range(3)]
    await provider.started.wait()
    provider.release.set()
    first_outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    retried = await analyzer.analyze(_snapshot())

    assert all(
        isinstance(outcome, ForgeException) and outcome.code is ForgeErrorCode.MODEL_UNAVAILABLE
        for outcome in first_outcomes
    )
    assert retried.analysis == ChapterAnalysis(core_idea="The central idea.")
    assert retried.cache_hit is False
    assert len(provider.calls) == 2
    assert cache.store_calls == 1


@pytest.mark.anyio
async def test_analyze_cancelled_waiter_does_not_cancel_shared_attempt_or_leave_registry() -> None:
    provider = FakeProvider([_valid_value()])
    provider.release = asyncio.Event()
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)

    first = asyncio.create_task(analyzer.analyze(_snapshot()))
    await provider.started.wait()
    cancelled_waiter = asyncio.create_task(analyzer.analyze(_snapshot()))
    await asyncio.sleep(0)
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    provider.release.set()
    completed = await first
    await asyncio.sleep(0)

    assert completed.analysis == ChapterAnalysis(core_idea="The central idea.")
    assert len(provider.calls) == 1
    assert cache.store_calls == 1
    assert analyzer._lock_entries == {}  # noqa: SLF001 - lifecycle regression assertion


@pytest.mark.anyio
async def test_analyze_cancelled_initiator_keeps_shared_attempt_for_other_waiter() -> None:
    provider = FakeProvider([_valid_value()])
    provider.release = asyncio.Event()
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)

    initiating = asyncio.create_task(analyzer.analyze(_snapshot()))
    await provider.started.wait()
    joined_waiter = asyncio.create_task(analyzer.analyze(_snapshot()))
    await asyncio.sleep(0)
    initiating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await initiating
    provider.release.set()
    completed = await joined_waiter
    await asyncio.sleep(0)

    assert completed.analysis == ChapterAnalysis(core_idea="The central idea.")
    assert len(provider.calls) == 1
    assert cache.store_calls == 1
    assert analyzer._lock_entries == {}  # noqa: SLF001 - lifecycle regression assertion


@pytest.mark.anyio
async def test_analyze_cancelled_only_initiator_finishes_and_cleans_shared_attempt() -> None:
    provider = FakeProvider([_valid_value()])
    provider.release = asyncio.Event()
    cache = FakeCache()
    analyzer = _analyzer(provider, cache)

    initiating = asyncio.create_task(analyzer.analyze(_snapshot()))
    await provider.started.wait()
    initiating.cancel()
    with pytest.raises(asyncio.CancelledError):
        await initiating
    provider.release.set()
    for _ in range(3):
        await asyncio.sleep(0)
    reused = await analyzer.analyze(_snapshot())

    assert reused.analysis == ChapterAnalysis(core_idea="The central idea.")
    assert reused.cache_hit is True
    assert len(provider.calls) == 1
    assert cache.store_calls == 1
    assert analyzer._lock_entries == {}  # noqa: SLF001 - lifecycle regression assertion
