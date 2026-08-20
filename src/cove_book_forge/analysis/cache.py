"""Cache boundary for reusable chapter analyses."""

from typing import Protocol, runtime_checkable

from cove_book_forge.contracts.analysis import ChapterAnalysis


@runtime_checkable
class ChapterAnalysisCache(Protocol):
    """Load and store validated analyses using the stable chapter cache key."""

    def load_chapter_analysis(
        self,
        source_system: str,
        external_book_id: str,
        chapter_index: int,
        input_fingerprint: str,
    ) -> ChapterAnalysis | None: ...

    def store_chapter_analysis(
        self,
        source_system: str,
        external_book_id: str,
        chapter_index: int,
        input_fingerprint: str,
        analysis: ChapterAnalysis,
    ) -> None: ...
