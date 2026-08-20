from __future__ import annotations

import os
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest
from pydantic import JsonValue

from cove_book_forge.analysis import ChapterAnalyzer
from cove_book_forge.config import AnalysisConfig, AppConfig
from cove_book_forge.contracts import AnalyzedChapter, ChapterAnalysis, ChapterSnapshot
from cove_book_forge.library import BookLibrary, LibraryDatabase, LibraryRepository
from cove_book_forge.providers import (
    JsonGeneration,
    ProviderCapabilities,
    ProviderUsage,
    TextGeneration,
)


class FakeProvider:
    def __init__(self, responses: list[dict[str, JsonValue]]) -> None:
        self._responses: deque[dict[str, JsonValue]] = deque(responses)
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
        return JsonGeneration(
            value=self._responses.popleft(),
            model="fake",
            usage=ProviderUsage(),
        )

    async def healthcheck(self) -> None:
        return None


def _config(data_dir: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "library": {"enabled": False, "data_dir": data_dir},
            "model": {"provider": "local", "model": "chapter-model"},
        }
    )


@contextmanager
def _open_library(config: AppConfig) -> Iterator[BookLibrary]:
    database = LibraryDatabase(config.library.data_dir / "library.sqlite3")
    repository = LibraryRepository(database)
    library = BookLibrary(config, repository=repository)
    library.initialize()
    try:
        yield library
    finally:
        database._close_connection()  # noqa: SLF001 - explicit real SQLite test lifecycle
        if library._books_fd is not None:  # noqa: SLF001 - explicit owned descriptor lifecycle
            os.close(library._books_fd)  # noqa: SLF001
            library._books_fd = None  # noqa: SLF001
        if library._data_root_fd is not None:  # noqa: SLF001
            os.close(library._data_root_fd)  # noqa: SLF001
            library._data_root_fd = None  # noqa: SLF001


def _snapshot(*, note: str = "记住：证据来自原文。") -> ChapterSnapshot:
    return ChapterSnapshot.model_validate(
        {
            "source_system": "unicode-reader",
            "external_book_id": "书籍-稳定-id",
            "book": {"title": "栖渡测试书", "language": "zh", "total_chapters": 1},
            "chapter": {
                "index": 0,
                "title": "第一章：可复用分析",
                "content": "完整 Unicode 正文：咖啡、模型与证据。",
                "source_locator": "reader:章节:0",
            },
            "highlights": [{"id": "高亮-1", "text": "模型与证据"}],
            "user_notes": [{"id": "笔记-1", "text": note}],
            "annotations": [{"id": "批注-1", "text": "编辑视角"}],
            "reflections": [{"id": "感想-1", "text": "保持可追溯"}],
        }
    )


def _consumer(label: str, analyzed: AnalyzedChapter) -> tuple[str, ChapterAnalysis]:
    return label, analyzed.analysis


@pytest.mark.anyio
async def test_disabled_library_analysis_persists_across_service_lifecycles(tmp_path: Path) -> None:
    """Removing SQLite persistence must cause the recreated analyzer to call Provider."""
    config = _config(tmp_path / "disabled-library")
    snapshot = _snapshot()
    generated_provider = FakeProvider([{"core_idea": "Unicode 分析结果", "topic_tags": ["证据"]}])

    with _open_library(config) as first_library:
        generated = await ChapterAnalyzer(
            generated_provider,
            first_library,
            AnalysisConfig(),
            config.model,
        ).analyze(snapshot)

    zero_call_provider = FakeProvider([])
    with _open_library(config) as recreated_library:
        reused = await ChapterAnalyzer(
            zero_call_provider,
            recreated_library,
            AnalysisConfig(),
            config.model,
        ).analyze(snapshot)

        changed_provider = FakeProvider([{"core_idea": "笔记变更后的结果"}])
        changed = await ChapterAnalyzer(
            changed_provider,
            recreated_library,
            AnalysisConfig(),
            config.model,
        ).analyze(_snapshot(note="只改变这一条笔记。"))

        with recreated_library._repository._database.connect() as connection:  # noqa: SLF001
            stored_rows = connection.execute(
                "SELECT input_fingerprint, analysis_json FROM chapter_analyses"
            ).fetchall()

    assert generated.cache_hit is False
    assert reused.cache_hit is True
    assert reused.analysis == generated.analysis
    assert zero_call_provider.calls == []
    assert changed.cache_hit is False
    assert len(changed_provider.calls) == 1
    assert changed.input_fingerprint != generated.input_fingerprint
    assert len(stored_rows) == 1
    assert stored_rows[0]["input_fingerprint"] == changed.input_fingerprint
    assert "笔记变更后的结果" in stored_rows[0]["analysis_json"]


@pytest.mark.anyio
async def test_ob_and_skill_placeholders_read_the_same_cached_chapter_analysis(
    tmp_path: Path,
) -> None:
    """Analyzing separately per future consumer must cause an extra Provider call."""
    config = _config(tmp_path / "shared-consumers")
    provider = FakeProvider([{"core_idea": "One reusable analysis"}])
    snapshot = _snapshot()

    with _open_library(config) as library:
        analyzer = ChapterAnalyzer(provider, library, AnalysisConfig(), config.model)
        ob_result = await analyzer.analyze(snapshot)
        skill_result = await analyzer.analyze(snapshot)
        ob_read = _consumer("OB", ob_result)
        skill_read = _consumer("Skill", skill_result)

    assert ob_read == ("OB", ChapterAnalysis(core_idea="One reusable analysis"))
    assert skill_read == ("Skill", ob_read[1])
    assert ob_result.input_fingerprint == skill_result.input_fingerprint
    assert skill_result.cache_hit is True
    assert len(provider.calls) == 1
