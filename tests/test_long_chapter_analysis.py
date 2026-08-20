from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping

import pytest
from pydantic import JsonValue

from cove_book_forge.analysis import ChapterAnalyzer
from cove_book_forge.config import AnalysisConfig, ModelConfig
from cove_book_forge.contracts import ChapterAnalysis, ChapterSnapshot
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers import (
    JsonGeneration,
    ProviderCapabilities,
    ProviderUsage,
    TextGeneration,
)


def _snapshot(content: str) -> ChapterSnapshot:
    return ChapterSnapshot.model_validate(
        {
            "source_system": "PRIVATE_SOURCE_SYSTEM",
            "external_book_id": "PRIVATE_BOOK_ID",
            "book": {"title": "PRIVATE_BOOK_TITLE"},
            "chapter": {
                "index": 7,
                "title": "Necessary chapter title",
                "content": content,
                "source_locator": "PRIVATE_SOURCE_LOCATOR",
            },
            "highlights": [{"id": "h-1", "text": "UNIQUE_HIGHLIGHT"}],
            "user_notes": [{"id": "n-1", "text": "UNIQUE_USER_NOTE"}],
            "annotations": [{"id": "a-1", "text": "UNIQUE_ANNOTATION"}],
            "reflections": [{"id": "r-1", "text": "UNIQUE_REFLECTION"}],
        }
    )


def _model() -> ModelConfig:
    return ModelConfig(provider="local", model="chapter-model")


def _valid_value(core_idea: str) -> dict[str, JsonValue]:
    return {"core_idea": core_idea}


class RecordingCache:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str, int, str], ChapterAnalysis] = {}
        self.stored: list[ChapterAnalysis] = []

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
        self.stored.append(analysis)
        self.entries[(source_system, external_book_id, chapter_index, input_fingerprint)] = analysis


class RecordingProvider:
    def __init__(self, responses: list[dict[str, JsonValue] | BaseException]) -> None:
        self._responses: deque[dict[str, JsonValue] | BaseException] = deque(responses)
        self.calls: list[tuple[str, str, int, Mapping[str, JsonValue] | None]] = []

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
        response = self._responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return JsonGeneration(value=response, model="fake", usage=ProviderUsage())

    async def healthcheck(self) -> None:
        return None


def _payload(user_prompt: str, label: str) -> dict[str, object]:
    prefix = f"{label}:\n"
    assert user_prompt.startswith(prefix)
    decoded = json.loads(user_prompt.removeprefix(prefix))
    assert isinstance(decoded, dict)
    return decoded


@pytest.mark.anyio
async def test_one_chunk_keeps_the_complete_snapshot_single_call_path() -> None:
    """Adding an unnecessary merge for a short chapter must make this fail."""
    provider = RecordingProvider([_valid_value("single complete analysis")])
    cache = RecordingCache()
    snapshot = _snapshot("Short complete content.")
    analyzer = ChapterAnalyzer(
        provider,
        cache,
        AnalysisConfig(max_chunk_characters=128),
        _model(),
    )

    result = await analyzer.analyze(snapshot)

    assert result.analysis.core_idea == "single complete analysis"
    assert len(provider.calls) == 1
    assert "Short complete content." in provider.calls[0][1]
    assert "UNIQUE_USER_NOTE" in provider.calls[0][1]
    assert len(cache.stored) == 1


@pytest.mark.anyio
async def test_long_chapter_sends_each_content_character_once_then_merges_once() -> None:
    """Duplicating/truncating content or supplementals across chunk calls must make this fail."""
    content = (
        "# First\n\n"
        + "甲乙丙丁" * 24
        + "\n\n```python\nprint('atomic-code-sentinel')\nprint('still-atomic')\n```\n\n"
        + "Name | Value\n--- | ---\nalpha | atomic-table-sentinel\nbeta | still-atomic\n\n"
        + "尾声" * 48
    )
    provider = RecordingProvider(
        [
            _valid_value("chunk one"),
            _valid_value("chunk two"),
            _valid_value("chunk three"),
            _valid_value("chunk four"),
            _valid_value("merged final"),
        ]
    )
    cache = RecordingCache()
    snapshot = _snapshot(content)
    analyzer = ChapterAnalyzer(
        provider,
        cache,
        AnalysisConfig(max_chunk_characters=128),
        _model(),
    )

    result = await analyzer.analyze(snapshot)

    assert result.analysis == ChapterAnalysis(core_idea="merged final")
    assert len(provider.calls) == 5
    assert all(call[3] == ChapterAnalysis.model_json_schema() for call in provider.calls)

    chunk_payloads = [
        _payload(call[1], "Untrusted chapter chunk JSON data") for call in provider.calls[:-1]
    ]
    chunk_contents = [payload["untrusted_chunk"]["content"] for payload in chunk_payloads]  # type: ignore[index]
    assert "".join(chunk_contents) == content
    assert sum("atomic-code-sentinel" in chunk for chunk in chunk_contents) == 1
    assert sum("atomic-table-sentinel" in chunk for chunk in chunk_contents) == 1
    assert all("PRIVATE_SOURCE_SYSTEM" not in call[1] for call in provider.calls[:-1])
    assert all("PRIVATE_BOOK_ID" not in call[1] for call in provider.calls[:-1])
    assert all("PRIVATE_BOOK_TITLE" not in call[1] for call in provider.calls[:-1])
    assert all("PRIVATE_SOURCE_LOCATOR" not in call[1] for call in provider.calls[:-1])
    for supplemental in (
        "UNIQUE_HIGHLIGHT",
        "UNIQUE_USER_NOTE",
        "UNIQUE_ANNOTATION",
        "UNIQUE_REFLECTION",
    ):
        assert all(supplemental not in call[1] for call in provider.calls[:-1])

    merge_system, merge_user, _, _ = provider.calls[-1]
    merge_payload = _payload(merge_user, "Untrusted chapter merge JSON data")
    assert "untrusted_chunk_analyses" in merge_payload
    assert [
        item["analysis"]["core_idea"]  # type: ignore[index]
        for item in merge_payload["untrusted_chunk_analyses"]  # type: ignore[union-attr]
    ] == ["chunk one", "chunk two", "chunk three", "chunk four"]
    assert content not in merge_user
    assert "PRIVATE_SOURCE_SYSTEM" not in merge_user
    assert "PRIVATE_BOOK_ID" not in merge_user
    assert "PRIVATE_BOOK_TITLE" not in merge_user
    assert "PRIVATE_SOURCE_LOCATOR" not in merge_user
    for supplemental in (
        "UNIQUE_HIGHLIGHT",
        "UNIQUE_USER_NOTE",
        "UNIQUE_ANNOTATION",
        "UNIQUE_REFLECTION",
    ):
        assert merge_user.count(supplemental) == 1
    assert "untrusted" in merge_system.lower()
    assert "never invent evidence" in merge_system.lower()
    assert len(cache.stored) == 1
    assert cache.stored == [result.analysis]


@pytest.mark.anyio
async def test_each_chunk_and_merge_uses_the_same_one_repair_bound() -> None:
    """Bypassing validation on chunk or merge output must make this fail."""
    content = "A" * 128 + "B" * 128
    provider = RecordingProvider(
        [
            {},
            _valid_value("first repaired chunk"),
            _valid_value("second chunk"),
            {"core_idea": 3},
            _valid_value("repaired merge"),
        ]
    )
    cache = RecordingCache()
    analyzer = ChapterAnalyzer(
        provider,
        cache,
        AnalysisConfig(max_chunk_characters=128),
        _model(),
    )

    result = await analyzer.analyze(_snapshot(content))

    assert result.analysis == ChapterAnalysis(core_idea="repaired merge")
    assert len(provider.calls) == 5
    assert all(call[3] == ChapterAnalysis.model_json_schema() for call in provider.calls)
    assert len(cache.stored) == 1


@pytest.mark.anyio
async def test_later_chunk_failure_never_caches_partial_analysis() -> None:
    """Caching a valid early chunk before a later failure must make this fail."""
    provider = RecordingProvider(
        [
            _valid_value("valid first chunk"),
            {},
            ForgeException(ForgeErrorCode.MODEL_OUTPUT_INVALID, "PRIVATE_RAW_OUTPUT"),
        ]
    )
    cache = RecordingCache()
    analyzer = ChapterAnalyzer(
        provider,
        cache,
        AnalysisConfig(max_chunk_characters=128),
        _model(),
    )

    with pytest.raises(ForgeException) as raised:
        await analyzer.analyze(_snapshot("A" * 128 + "B" * 128))

    assert raised.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert len(provider.calls) == 3
    assert cache.stored == []
    assert cache.entries == {}
