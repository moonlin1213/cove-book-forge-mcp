import pytest
from pydantic import ValidationError

from cove_book_forge.contracts.books import (
    BookMetadata,
    ChapterContent,
    ChapterSnapshot,
    ExternalIdentity,
    Highlight,
)


def test_external_snapshot_keeps_generic_reading_context() -> None:
    snapshot = ChapterSnapshot(
        source_system="reader",
        external_book_id="book-1",
        book=BookMetadata(title="A Book", author="An Author", total_chapters=3),
        chapter=ChapterContent(index=0, title="Opening", content="Real content."),
        highlights=[Highlight(id="h-1", text="Real content.", paragraph_index=0)],
    )
    assert snapshot.external_identity == ExternalIdentity(
        source_system="reader", external_book_id="book-1"
    )
    assert snapshot.chapter.index == 0
    assert snapshot.highlights[0].id == "h-1"
    payload = snapshot.model_dump(mode="json")
    assert payload["source_system"] == "reader"
    assert "identity" not in payload


def test_snapshot_rejects_empty_content_and_private_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ChapterContent(index=0, title="Opening", content="")
    with pytest.raises(ValidationError):
        BookMetadata(title="A Book", moon_private=True)
