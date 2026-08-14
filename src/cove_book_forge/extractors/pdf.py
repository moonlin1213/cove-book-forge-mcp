from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from pypdf import PdfReader

from cove_book_forge.contracts import (
    BookFormat,
    BookMetadata,
    ChapterContent,
    ExtractedBook,
    PdfProfile,
)
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors.sanitize import sanitize_text
from cove_book_forge.extractors.security import ExtractionLimits

_EDGE_FRACTION = 0.12
_REPEATED_EDGE_MIN_PAGES = 3
_FALLBACK_BLOCK_PAGES = 20
_MIN_MEANINGFUL_LETTERS = 2
_PAGE_NUMBER = re.compile(r"(?i)(?:page\s+)?(?:\d{1,6}|[ivxlcdm]{1,12})")
_PAGINATION_LINE = re.compile(r"(?i)(?:page\s+)?\d{1,6}\s*(?:of|/)\s*\d{1,6}")
_DEFINITION_LINE = re.compile(
    r"^(?:(?:async\s+)?(?:def|function)\s+[A-Za-z_]\w*\s*\([^)]*\)\s*(?::|\{)"
    r"|class\s+[A-Za-z_]\w*(?:\s*\([^)]*\))?\s*(?::|\{))$"
)
_CONTROL_LINE = re.compile(
    r"^(?:(?:if|elif|while|for|with)\s+(?:\([^)]*\)|.+):|(?:if|while|for|switch)\s*\([^)]*\)\s*\{)$"
)
_IMPORT_LINE = re.compile(
    r"^(?:import\s+[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\s+as\s+[A-Za-z_]\w*)?"
    r"|from\s+[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\s+import\s+(?:[A-Za-z_*]\w*|\*))$"
)
_INDENTED_OPERATOR_LINE = re.compile(
    r"^[ \t]{2,}(?:(?:return|yield)\b.*(?:[+*/^−-]|==|!=|<=|>=|:=)"
    r"|[A-Za-z_]\w*(?:(?:\.[A-Za-z_]\w*)|(?:\[[^]]+\]))*\s*(?:[+*/-]?=|:=)\s*\S)"
)
_FORMULA_LINE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*\s*=\s*[^=].*[+*/^−-]")


class LayoutPdfExtractor(Protocol):
    """Future extension point for an explicitly supplied layout-aware parser."""

    def extract(self, source: Path, fingerprint: str) -> ExtractedBook: ...


@dataclass(frozen=True, slots=True)
class _PageText:
    raw_text: str
    top_edge: frozenset[str]
    bottom_edge: frozenset[str]


@dataclass(frozen=True, slots=True)
class _OutlineBoundary:
    title: str
    page_index: int


def _failure(code: ForgeErrorCode = ForgeErrorCode.EXTRACTION_FAILED) -> ForgeException:
    return ForgeException(code, "PDF extraction failed.")


def _normalized_line(value: str) -> str:
    return sanitize_text(value).rstrip()


def _safe_text(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(sanitize_text(value).split())[:maximum]


def _extract_page(page: Any) -> _PageText:
    top_edge: set[str] = set()
    bottom_edge: set[str] = set()
    height = float(page.mediabox.height)

    def visit_text(
        text: str,
        _current_matrix: Sequence[float],
        text_matrix: Sequence[float],
        _font_dictionary: Any,
        _font_size: float,
    ) -> None:
        if not text.strip() or len(text_matrix) < 6:
            return
        y_position = float(text_matrix[5])
        target = top_edge if y_position >= height * (1 - _EDGE_FRACTION) else None
        if y_position <= height * _EDGE_FRACTION:
            target = bottom_edge
        if target is not None:
            target.update(
                line
                for part in text.splitlines()
                if (line := _normalized_line(part)) and line.strip()
            )

    try:
        raw_text = page.extract_text(visitor_text=visit_text) or ""
    except Exception:
        # Positional inspection is optional cleanup. Retry without it so a failure at
        # the page edge cannot silently discard otherwise extractable main text.
        raw_text = page.extract_text() or ""
        top_edge.clear()
        bottom_edge.clear()
    return _PageText(
        raw_text=sanitize_text(raw_text),
        top_edge=frozenset(top_edge),
        bottom_edge=frozenset(bottom_edge),
    )


def _repeated_edge_values(pages: Sequence[_PageText], *, top: bool) -> frozenset[str]:
    counts: Counter[str] = Counter()
    for page in pages:
        counts.update(page.top_edge if top else page.bottom_edge)
    minimum = max(_REPEATED_EDGE_MIN_PAGES, (len(pages) + 1) // 2)
    return frozenset(value for value, count in counts.items() if count >= minimum)


def _clean_page(
    page: _PageText,
    *,
    repeated_top: frozenset[str],
    repeated_bottom: frozenset[str],
) -> str:
    lines = [
        line
        for raw_line in page.raw_text.splitlines()
        if (line := _normalized_line(raw_line)) and line.strip()
    ]
    if not lines:
        return ""

    start = 0
    while start < len(lines) and lines[start] in page.top_edge:
        line = lines[start]
        if line not in repeated_top and not _PAGE_NUMBER.fullmatch(line.strip()):
            break
        start += 1

    end = len(lines)
    while end > start and lines[end - 1] in page.bottom_edge:
        line = lines[end - 1]
        if line not in repeated_bottom and not _PAGE_NUMBER.fullmatch(line.strip()):
            break
        end -= 1
    return "\n".join(lines[start:end])


def _has_meaningful_text(page_texts: Sequence[str]) -> bool:
    # Two Unicode letters outside complete pagination and numeric/punctuation-only
    # lines reject scan noise while accepting genuinely tiny prose such as "Hi".
    candidate_lines = (
        line
        for text in page_texts
        for line in text.splitlines()
        if not _is_meaningless_text_line(line)
    )
    return sum(character.isalpha() for line in candidate_lines for character in line) >= (
        _MIN_MEANINGFUL_LETTERS
    )


def _is_meaningless_text_line(line: str) -> bool:
    stripped = line.strip()
    return bool(
        not stripped
        or _PAGE_NUMBER.fullmatch(stripped)
        or _PAGINATION_LINE.fullmatch(stripped)
        or not any(character.isalpha() for character in stripped)
    )


def _flatten_outline(items: Iterable[Any]) -> Iterable[Any]:
    for item in items:
        if isinstance(item, list):
            yield from _flatten_outline(item)
        else:
            yield item


def _outline_boundaries(reader: PdfReader, page_count: int) -> list[_OutlineBoundary]:
    boundaries: list[_OutlineBoundary] = []
    last_page = -1
    try:
        outline = reader.outline
    except Exception:
        return []
    for item in _flatten_outline(outline):
        try:
            page_index = reader.get_destination_page_number(item)
            title = _safe_text(getattr(item, "title", ""), maximum=500)
        except Exception:
            continue
        if (
            page_index is None
            or page_index < 0
            or page_index >= page_count
            or page_index <= last_page
            or not title
        ):
            continue
        boundaries.append(_OutlineBoundary(title=title, page_index=page_index))
        last_page = page_index
    if boundaries and boundaries[0].page_index != 0:
        boundaries.insert(0, _OutlineBoundary(title="Front Matter", page_index=0))
    return boundaries


def _join_range(page_texts: Sequence[str], start: int, end: int) -> str:
    return "\n\n".join(text for text in page_texts[start:end] if text)


def _outline_chapters(
    page_texts: Sequence[str], boundaries: Sequence[_OutlineBoundary]
) -> list[ChapterContent]:
    chapters: list[ChapterContent] = []
    for position, boundary in enumerate(boundaries):
        end = (
            boundaries[position + 1].page_index
            if position + 1 < len(boundaries)
            else len(page_texts)
        )
        content = _join_range(page_texts, boundary.page_index, end)
        if not content:
            continue
        chapters.append(
            ChapterContent(
                index=len(chapters),
                title=boundary.title,
                content=content,
                source_locator=f"pdf:pages:{boundary.page_index + 1}-{end}",
            )
        )
    return chapters


def _fallback_chapters(page_texts: Sequence[str]) -> list[ChapterContent]:
    chapters: list[ChapterContent] = []
    for start in range(0, len(page_texts), _FALLBACK_BLOCK_PAGES):
        end = min(start + _FALLBACK_BLOCK_PAGES, len(page_texts))
        content = _join_range(page_texts, start, end)
        if not content:
            continue
        chapters.append(
            ChapterContent(
                index=len(chapters),
                title=f"Pages {start + 1}-{end}",
                content=content,
                source_locator=f"pdf:pages:{start + 1}-{end}",
            )
        )
    return chapters


def _pdf_profile(chapters: Sequence[ChapterContent]) -> PdfProfile:
    """Require two code, pipe-table, or assignment/formula lines for TECHNICAL."""
    signals = 0
    for line in (line for chapter in chapters for line in chapter.content.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        if _is_code_line(line) or stripped.count("|") >= 2 or _FORMULA_LINE.search(stripped):
            signals += 1
        if signals >= 2:
            return PdfProfile.TECHNICAL
    return PdfProfile.TEXT


def _is_code_line(line: str) -> bool:
    stripped = line.strip()
    return bool(
        _DEFINITION_LINE.fullmatch(stripped)
        or _CONTROL_LINE.fullmatch(stripped)
        or _IMPORT_LINE.fullmatch(stripped)
        or _INDENTED_OPERATOR_LINE.search(line)
    )


def _metadata(reader: PdfReader, source: Path) -> BookMetadata:
    try:
        pdf_metadata = reader.metadata
    except Exception:
        pdf_metadata = None
    title = _safe_text(getattr(pdf_metadata, "title", ""), maximum=500)
    author = _safe_text(getattr(pdf_metadata, "author", ""), maximum=300)
    if not title:
        title = _safe_text(source.stem, maximum=500) or "Untitled PDF"
    return BookMetadata(title=title, author=author)


class PdfExtractor:
    def __init__(self, *, limits: ExtractionLimits | None = None) -> None:
        self._limits = limits or ExtractionLimits()

    def extract(self, source: Path, fingerprint: str) -> ExtractedBook:
        try:
            reader = PdfReader(source, strict=False)
            if reader.is_encrypted:
                raise _failure(ForgeErrorCode.ENCRYPTED_DOCUMENT)
            page_count = len(reader.pages)
            if page_count > self._limits.max_pdf_pages:
                raise _failure()
            pages = [_extract_page(page) for page in reader.pages]
            repeated_top = _repeated_edge_values(pages, top=True)
            repeated_bottom = _repeated_edge_values(pages, top=False)
            page_texts = [
                _clean_page(
                    page,
                    repeated_top=repeated_top,
                    repeated_bottom=repeated_bottom,
                )
                for page in pages
            ]
            if not _has_meaningful_text(page_texts):
                raise _failure(ForgeErrorCode.OCR_REQUIRED)
            boundaries = _outline_boundaries(reader, page_count)
            chapters = (
                _outline_chapters(page_texts, boundaries)
                if boundaries
                else _fallback_chapters(page_texts)
            )
            if not chapters:
                raise _failure(ForgeErrorCode.OCR_REQUIRED)
            metadata = _metadata(reader, source)
            return ExtractedBook(
                format=BookFormat.PDF,
                metadata=metadata.model_copy(update={"total_chapters": len(chapters)}),
                chapters=tuple(chapters),
                source_fingerprint=fingerprint,
                pdf_profile=_pdf_profile(chapters),
            )
        except ForgeException:
            raise
        except Exception as exc:
            raise _failure() from exc
