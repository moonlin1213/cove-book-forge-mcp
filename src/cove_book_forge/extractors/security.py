import hashlib
from dataclasses import dataclass
from pathlib import Path

from cove_book_forge.contracts.ingestion import BookFormat
from cove_book_forge.errors import ForgeErrorCode, ForgeException

_HASH_CHUNK_SIZE = 1024 * 1024
_PDF_MAGIC = b"%PDF-"
_EPUB_MAGIC = b"PK\x03\x04"


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    max_source_bytes: int = 512 * 1024 * 1024
    max_pdf_pages: int = 5_000
    max_zip_members: int = 10_000
    max_expanded_zip_bytes: int = 1024 * 1024 * 1024
    max_zip_member_bytes: int = 128 * 1024 * 1024
    max_compression_ratio: int = 100


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    fingerprint: str

    @classmethod
    def capture(cls, source: Path, *, limits: ExtractionLimits | None = None) -> "SourceSnapshot":
        validate_source(source, limits=limits)
        return cls(fingerprint=fingerprint_source(source, limits=limits))


def _source_error(code: ForgeErrorCode) -> ForgeException:
    return ForgeException(code, "Source validation failed.")


def validate_source(source: Path, *, limits: ExtractionLimits | None = None) -> None:
    limits = limits or ExtractionLimits()
    try:
        is_regular = source.is_file()
    except OSError as exc:
        raise _source_error(ForgeErrorCode.SOURCE_NOT_FOUND) from exc
    if not is_regular:
        raise _source_error(ForgeErrorCode.SOURCE_NOT_FOUND)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise _source_error(ForgeErrorCode.SOURCE_NOT_FOUND) from exc
    if size > limits.max_source_bytes:
        raise _source_error(ForgeErrorCode.EXTRACTION_FAILED)


def fingerprint_source(source: Path, *, limits: ExtractionLimits | None = None) -> str:
    limits = limits or ExtractionLimits()
    digest = hashlib.sha256()
    bytes_read = 0
    try:
        with source.open("rb") as stream:
            while chunk := stream.read(min(_HASH_CHUNK_SIZE, limits.max_source_bytes - bytes_read + 1)):
                bytes_read += len(chunk)
                if bytes_read > limits.max_source_bytes:
                    raise _source_error(ForgeErrorCode.EXTRACTION_FAILED)
                digest.update(chunk)
    except OSError as exc:
        raise _source_error(ForgeErrorCode.SOURCE_NOT_FOUND) from exc
    return digest.hexdigest()


def detect_book_format(source: Path) -> BookFormat:
    suffix = source.suffix.lower()
    if suffix not in {".epub", ".pdf"}:
        raise _source_error(ForgeErrorCode.UNSUPPORTED_FORMAT)
    try:
        with source.open("rb") as stream:
            magic = stream.read(max(len(_PDF_MAGIC), len(_EPUB_MAGIC)))
    except OSError as exc:
        raise _source_error(ForgeErrorCode.SOURCE_NOT_FOUND) from exc
    if suffix == ".pdf" and magic.startswith(_PDF_MAGIC):
        return BookFormat.PDF
    if suffix == ".epub" and magic.startswith(_EPUB_MAGIC):
        return BookFormat.EPUB
    raise _source_error(ForgeErrorCode.UNSUPPORTED_FORMAT)


def ensure_source_unchanged(
    source: Path, snapshot: SourceSnapshot, *, limits: ExtractionLimits | None = None
) -> None:
    try:
        validate_source(source, limits=limits)
        fingerprint = fingerprint_source(source, limits=limits)
    except ForgeException as exc:
        raise _source_error(ForgeErrorCode.SOURCE_CHANGED) from exc
    if fingerprint != snapshot.fingerprint:
        raise _source_error(ForgeErrorCode.SOURCE_CHANGED)
