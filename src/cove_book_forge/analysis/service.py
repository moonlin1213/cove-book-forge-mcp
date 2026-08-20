"""Single-chapter structured analysis with bounded repair and cache reuse."""

import asyncio
import json
from dataclasses import dataclass

from pydantic import ValidationError

from cove_book_forge.analysis.cache import ChapterAnalysisCache
from cove_book_forge.analysis.chunks import split_chapter_content
from cove_book_forge.analysis.fingerprint import (
    canonical_analysis_source_payload,
    chapter_input_fingerprint,
)
from cove_book_forge.analysis.prompts import (
    build_chapter_analysis_prompts,
    build_chapter_chunk_analysis_prompts,
    build_chapter_merge_prompts,
)
from cove_book_forge.config.models import AnalysisConfig, ModelConfig
from cove_book_forge.contracts.analysis import AnalyzedChapter, ChapterAnalysis
from cove_book_forge.contracts.books import ChapterSnapshot
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers.base import ModelProvider


@dataclass
class _AttemptOutcome:
    analysis: ChapterAnalysis
    from_cache: bool


@dataclass
class _InflightAttempt:
    task: asyncio.Task[_AttemptOutcome]


class ChapterAnalyzer:
    """Generate exactly one validated analysis for a normalized chapter snapshot."""

    def __init__(
        self,
        provider: ModelProvider,
        cache: ChapterAnalysisCache,
        analysis_config: AnalysisConfig,
        model_config: ModelConfig,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._analysis_config = analysis_config
        self._model_config = model_config
        self._lock_entries: dict[tuple[str, str, int, str], _InflightAttempt] = {}
        self._lock_entries_guard = asyncio.Lock()

    async def analyze(self, snapshot: ChapterSnapshot, *, force: bool = False) -> AnalyzedChapter:
        """Return a cached result or generate and persist one strict analysis."""
        input_fingerprint = chapter_input_fingerprint(
            snapshot, self._analysis_config, self._model_config
        )
        key = (
            snapshot.source_system,
            snapshot.external_book_id,
            snapshot.chapter.index,
            input_fingerprint,
        )

        if not force:
            cached = self._cache.load_chapter_analysis(*key)
            if cached is not None:
                return AnalyzedChapter(
                    analysis=cached,
                    input_fingerprint=input_fingerprint,
                    cache_hit=True,
                )

        while True:
            entry, started_attempt = await self._join_attempt(key, snapshot, force=force)
            outcome = await asyncio.shield(entry.task)
            if force and outcome.from_cache:
                continue
            return AnalyzedChapter(
                analysis=outcome.analysis,
                input_fingerprint=input_fingerprint,
                cache_hit=outcome.from_cache or not started_attempt,
            )

    async def _join_attempt(
        self,
        key: tuple[str, str, int, str],
        snapshot: ChapterSnapshot,
        *,
        force: bool,
    ) -> tuple[_InflightAttempt, bool]:
        async with self._lock_entries_guard:
            existing = self._lock_entries.get(key)
            if existing is not None and not existing.task.done():
                return existing, False

            task = asyncio.create_task(self._generate_and_store(snapshot, key, force=force))
            entry = _InflightAttempt(task=task)
            self._lock_entries[key] = entry
            task.add_done_callback(lambda completed: self._on_attempt_done(key, entry, completed))
            return entry, True

    async def _generate_and_store(
        self,
        snapshot: ChapterSnapshot,
        key: tuple[str, str, int, str],
        *,
        force: bool,
    ) -> _AttemptOutcome:
        if not force:
            cached = self._cache.load_chapter_analysis(*key)
            if cached is not None:
                return _AttemptOutcome(analysis=cached, from_cache=True)
        analysis = await self._generate_valid_analysis(snapshot)
        self._cache.store_chapter_analysis(*key, analysis)
        return _AttemptOutcome(analysis=analysis, from_cache=False)

    async def _generate_valid_analysis(self, snapshot: ChapterSnapshot) -> ChapterAnalysis:
        canonical_source = canonical_analysis_source_payload(snapshot)
        canonical_chapter = canonical_source["chapter"]
        chunks = split_chapter_content(
            canonical_chapter["content"],
            self._analysis_config.max_chunk_characters,
        )
        if len(chunks) == 1:
            return await self._generate_valid_analysis_from_prompts(
                *build_chapter_analysis_prompts(snapshot)
            )

        chunk_analyses: list[ChapterAnalysis] = []
        for chunk_number, chunk in enumerate(chunks, start=1):
            chunk_analyses.append(
                await self._generate_valid_analysis_from_prompts(
                    *build_chapter_chunk_analysis_prompts(
                        chapter_title=canonical_chapter["title"],
                        chunk_content=chunk,
                        chunk_number=chunk_number,
                        chunk_count=len(chunks),
                    )
                )
            )
        return await self._generate_valid_analysis_from_prompts(
            *build_chapter_merge_prompts(snapshot, tuple(chunk_analyses))
        )

    async def _generate_valid_analysis_from_prompts(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> ChapterAnalysis:
        schema = ChapterAnalysis.model_json_schema()
        for attempt in range(2):
            try:
                generation = await self._provider.generate_json(
                    system_prompt,
                    user_prompt,
                    max_output_tokens=self._model_config.default_max_output_tokens,
                    json_schema=schema,
                )
            except ForgeException as exc:
                if exc.code is not ForgeErrorCode.MODEL_OUTPUT_INVALID:
                    raise
            except Exception:
                raise ForgeException(
                    ForgeErrorCode.MODEL_UNAVAILABLE,
                    "Chapter analysis provider call failed.",
                ) from None
            else:
                try:
                    return ChapterAnalysis.model_validate_json(
                        json.dumps(generation.value, ensure_ascii=False, allow_nan=False),
                        strict=True,
                    )
                except (TypeError, ValueError, ValidationError):
                    pass
            if attempt == 1:
                raise ForgeException(
                    ForgeErrorCode.MODEL_OUTPUT_INVALID,
                    "Chapter analysis response did not match the required schema.",
                )
        raise AssertionError("bounded analysis attempts must return or raise")

    def _on_attempt_done(
        self,
        key: tuple[str, str, int, str],
        entry: _InflightAttempt,
        completed: asyncio.Task[_AttemptOutcome],
    ) -> None:
        if not completed.cancelled():
            completed.exception()
        asyncio.create_task(self._discard_finished_attempt(key, entry))

    async def _discard_finished_attempt(
        self,
        key: tuple[str, str, int, str],
        entry: _InflightAttempt,
    ) -> None:
        async with self._lock_entries_guard:
            if self._lock_entries.get(key) is entry:
                del self._lock_entries[key]
