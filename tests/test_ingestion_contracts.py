from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cove_book_forge.contracts import (
    BookFormat,
    BookMetadata,
    BookRef,
    ChapterContent,
    ExtractedBook,
    ImportedBook,
    ImportMode,
    PdfProfile,
    StoredBook,
)


def test_extracted_book_is_frozen_strict_and_serializes_enum_values() -> None:
    book = ExtractedBook(
        format=BookFormat.EPUB,
        metadata=BookMetadata(title="Synthetic book"),
        chapters=(ChapterContent(index=0, content="Synthetic content."),),
        source_fingerprint="a" * 64,
        pdf_profile=None,
    )

    assert book.model_dump(mode="json")["format"] == "epub"
    with pytest.raises(ValidationError):
        ExtractedBook(
            format=BookFormat.EPUB,
            metadata=BookMetadata(title="Synthetic book"),
            chapters=(),
            source_fingerprint="a" * 64,
            unexpected="private",
        )
    with pytest.raises(ValidationError):
        book.source_fingerprint = "b" * 64  # type: ignore[misc]


def test_imported_and_stored_books_expose_the_locked_import_contract() -> None:
    reference = BookRef(book_id="synthetic-book")
    metadata = BookMetadata(title="Synthetic book")
    imported = ImportedBook(
        book=reference,
        metadata=metadata,
        format=BookFormat.PDF,
        import_mode=ImportMode.REFERENCE,
        source_fingerprint="b" * 64,
    )
    stored = StoredBook(
        book=reference,
        metadata=metadata,
        source_fingerprint="c" * 64,
        source_available=True,
        created_at=datetime(2026, 8, 14, tzinfo=UTC),
        updated_at=datetime(2026, 8, 14, tzinfo=UTC),
    )

    assert imported.import_mode is ImportMode.REFERENCE
    assert imported.format is BookFormat.PDF
    assert PdfProfile.TECHNICAL.value == "technical"
    assert stored.format is None
    assert stored.import_mode is None


def test_ingestion_contracts_reject_non_lowercase_sha256_fingerprints() -> None:
    with pytest.raises(ValidationError, match="lowercase SHA-256"):
        ImportedBook(
            book=BookRef(book_id="synthetic-book"),
            metadata=BookMetadata(title="Synthetic book"),
            format=BookFormat.PDF,
            import_mode=ImportMode.COPY,
            source_fingerprint="A" * 64,
        )
