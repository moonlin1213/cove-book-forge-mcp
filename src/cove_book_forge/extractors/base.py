from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from cove_book_forge.contracts.ingestion import BookFormat, ExtractedBook
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors.security import (
    ExtractionLimits,
    SourceSnapshot,
    detect_book_format,
    ensure_source_unchanged,
)


class BookExtractor(Protocol):
    def extract(self, source: Path, fingerprint: str) -> ExtractedBook: ...


class BookExtractorRegistry:
    def __init__(
        self,
        extractors: Mapping[BookFormat, BookExtractor] | None = None,
        *,
        limits: ExtractionLimits | None = None,
    ) -> None:
        self._limits = limits or ExtractionLimits()
        self._extractors: dict[BookFormat, BookExtractor]
        if extractors is None:
            from cove_book_forge.extractors.epub import EpubExtractor
            from cove_book_forge.extractors.pdf import PdfExtractor

            self._extractors = {
                BookFormat.EPUB: EpubExtractor(limits=self._limits),
                BookFormat.PDF: PdfExtractor(limits=self._limits),
            }
        else:
            self._extractors = dict(extractors)

    @property
    def limits(self) -> ExtractionLimits:
        """Return the same immutable limits used for provenance validation."""
        return self._limits

    def get_for_source(self, source: Path) -> BookExtractor | None:
        return self._extractors.get(detect_book_format(source))

    def extract(self, source: Path) -> ExtractedBook:
        snapshot = SourceSnapshot.capture(source, limits=self._limits)
        extractor = self.get_for_source(source)
        if extractor is None:
            raise ForgeException(ForgeErrorCode.UNSUPPORTED_FORMAT, "No extractor is registered.")
        extracted = extractor.extract(source, snapshot.fingerprint)
        ensure_source_unchanged(source, snapshot, limits=self._limits)
        if extracted.source_fingerprint != snapshot.fingerprint:
            raise ForgeException(
                ForgeErrorCode.EXTRACTION_FAILED, "Extractor returned invalid provenance."
            )
        return extracted
