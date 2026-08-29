import codecs
from pathlib import Path

from cove_book_forge.contracts import BookFormat, BookMetadata, ChapterContent, ExtractedBook
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors.sanitize import sanitize_text
from cove_book_forge.extractors.security import ExtractionLimits


def _failure() -> ForgeException:
    return ForgeException(ForgeErrorCode.EXTRACTION_FAILED, "TXT extraction failed.")


def _decode(payload: bytes) -> str:
    encoding = (
        "utf-16" if payload.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
    )
    return payload.decode(encoding)


class TxtExtractor:
    def __init__(self, *, limits: ExtractionLimits | None = None) -> None:
        self._limits = limits or ExtractionLimits()

    def extract(self, source: Path, fingerprint: str) -> ExtractedBook:
        try:
            with source.open("rb") as stream:
                payload = stream.read(self._limits.max_source_bytes + 1)
            if len(payload) > self._limits.max_source_bytes:
                raise _failure()
            decoded = _decode(payload)
            if "\x00" in decoded:
                raise _failure()
            content = sanitize_text(decoded).replace("\r\n", "\n").replace("\r", "\n").strip()
            if not content:
                raise _failure()
            title = sanitize_text(source.stem).strip()[:500] or "Untitled Text"
            chapter = ChapterContent(
                index=0,
                title=title,
                content=content,
                source_locator="txt:document",
            )
            return ExtractedBook(
                format=BookFormat.TXT,
                metadata=BookMetadata(title=title, total_chapters=1),
                chapters=(chapter,),
                source_fingerprint=fingerprint,
            )
        except ForgeException:
            raise
        except (OSError, UnicodeError) as exc:
            raise _failure() from exc
