from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from cove_book_forge.contracts.ingestion import BookFormat, ExtractedBook
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors.security import (
    SourceSnapshot,
    detect_book_format,
    ensure_source_unchanged,
)


class BookExtractor(Protocol):
    def extract(self, source: Path, fingerprint: str) -> ExtractedBook: ...


class BookExtractorRegistry:
    def __init__(self, extractors: Mapping[BookFormat, BookExtractor] | None = None) -> None:
        self._extractors = dict(extractors or {})

    def get_for_source(self, source: Path) -> BookExtractor | None:
        return self._extractors.get(detect_book_format(source))

    def extract(self, source: Path) -> ExtractedBook:
        snapshot = SourceSnapshot.capture(source)
        extractor = self.get_for_source(source)
        if extractor is None:
            raise ForgeException(ForgeErrorCode.UNSUPPORTED_FORMAT, "No extractor is registered.")
        extracted = extractor.extract(source, snapshot.fingerprint)
        ensure_source_unchanged(source, snapshot)
        return extracted
