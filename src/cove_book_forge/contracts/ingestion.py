import re
from datetime import datetime
from enum import StrEnum

from pydantic import field_validator

from cove_book_forge.contracts.base import ContractModel
from cove_book_forge.contracts.books import BookMetadata, BookRef, ChapterContent

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

class BookFormat(StrEnum):
    EPUB = "epub"
    PDF = "pdf"


class ImportMode(StrEnum):
    COPY = "copy"
    REFERENCE = "reference"


class PdfProfile(StrEnum):
    TEXT = "text"
    TECHNICAL = "technical"


def _validate_fingerprint(value: str) -> str:
    if not _SHA256_PATTERN.fullmatch(value):
        msg = "source_fingerprint must be a lowercase SHA-256 digest"
        raise ValueError(msg)
    return value


class ExtractedBook(ContractModel):
    format: BookFormat
    metadata: BookMetadata
    chapters: tuple[ChapterContent, ...]
    source_fingerprint: str
    pdf_profile: PdfProfile | None = None

    _validate_source_fingerprint = field_validator("source_fingerprint")(_validate_fingerprint)

class ImportedBook(ContractModel):
    book: BookRef
    metadata: BookMetadata
    format: BookFormat
    import_mode: ImportMode
    source_fingerprint: str

    _validate_source_fingerprint = field_validator("source_fingerprint")(_validate_fingerprint)

class StoredBook(ContractModel):
    book: BookRef
    metadata: BookMetadata
    format: BookFormat | None = None
    import_mode: ImportMode | None = None
    source_fingerprint: str
    source_available: bool
    created_at: datetime
    updated_at: datetime

    _validate_source_fingerprint = field_validator("source_fingerprint")(_validate_fingerprint)
