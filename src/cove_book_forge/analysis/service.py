"""Single-chapter structured analysis with bounded repair and cache reuse."""

import asyncio
import json
from dataclasses import dataclass

from pydantic import ValidationError

from cove_book_forge.analysis.cache import ChapterAnalysisCache
from cove_book_forge.analysis.fingerprint import chapter_input_fingerprint
from cove_book_forge.analysis.prompts import build_chapter_analysis_prompts
from cove_book_forge.config.models import AnalysisConfig, ModelConfig
from cove_book_forge.contracts.analysis import AnalyzedChapter, ChapterAnalysis
from cove_book_forge.contracts.books import ChapterSnapshot
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.providers.base import ModelProvider


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0
    completed_generations: int = 0


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
        self._lock_entries: dict[tuple[str, str, int, str], _LockEntry] = {}
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

        entry, joined_generation = await self._reserve_lock(key)
        try:
            async with entry.lock:
                if not force or entry.completed_generations > joined_generation:
                    cached = self._cache.load_chapter_analysis(*key)
                    if cached is not None:
                        return AnalyzedChapter(
                            analysis=cached,
                            input_fingerprint=input_fingerprint,
                            cache_hit=True,
                        )

                analysis = await self._generate_valid_analysis(snapshot)
                self._cache.store_chapter_analysis(*key, analysis)
                entry.completed_generations += 1
                return AnalyzedChapter(
                    analysis=analysis,
                    input_fingerprint=input_fingerprint,
                    cache_hit=False,
                )
        finally:
            await self._release_lock(key, entry)

    async def _generate_valid_analysis(self, snapshot: ChapterSnapshot) -> ChapterAnalysis:
        system_prompt, user_prompt = build_chapter_analysis_prompts(snapshot)
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

    async def _reserve_lock(self, key: tuple[str, str, int, str]) -> tuple[_LockEntry, int]:
        async with self._lock_entries_guard:
            entry = self._lock_entries.get(key)
            if entry is None:
                entry = _LockEntry(lock=asyncio.Lock())
                self._lock_entries[key] = entry
            entry.users += 1
            return entry, entry.completed_generations

    async def _release_lock(self, key: tuple[str, str, int, str], entry: _LockEntry) -> None:
        async with self._lock_entries_guard:
            entry.users -= 1
            if (
                entry.users == 0
                and not entry.lock.locked()
                and self._lock_entries.get(key) is entry
            ):
                del self._lock_entries[key]
