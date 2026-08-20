from typing import Final

from pydantic import Field

from cove_book_forge.contracts.base import ContractModel

MAX_BOOK_CHAPTERS: Final = 5_000


class ExternalIdentity(ContractModel):
    source_system: str = Field(min_length=1, max_length=80)
    external_book_id: str = Field(min_length=1, max_length=240)


class BookMetadata(ContractModel):
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(default="", max_length=300)
    language: str = Field(default="", max_length=40)
    total_chapters: int = Field(default=0, ge=0, le=MAX_BOOK_CHAPTERS)


class BookRef(ContractModel):
    book_id: str = Field(min_length=1, max_length=120)


class ChapterContent(ContractModel):
    index: int = Field(ge=0, lt=MAX_BOOK_CHAPTERS)
    title: str = Field(default="", max_length=500)
    content: str = Field(min_length=1)
    source_locator: str = Field(default="", max_length=500)


class Highlight(ContractModel):
    id: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1)
    note: str = ""
    paragraph_index: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=1)


class UserNote(ContractModel):
    id: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1)
    paragraph_index: int | None = Field(default=None, ge=0)


class Annotation(ContractModel):
    id: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1)
    author_label: str = Field(default="", max_length=120)
    kind: str = Field(default="annotation", max_length=80)
    paragraph_index: int | None = Field(default=None, ge=0)


class Reflection(ContractModel):
    id: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1)
    author_label: str = Field(default="", max_length=120)


class ChapterSnapshot(ContractModel):
    source_system: str = Field(min_length=1, max_length=80)
    external_book_id: str = Field(min_length=1, max_length=240)
    book: BookMetadata
    chapter: ChapterContent
    highlights: tuple[Highlight, ...] = ()
    user_notes: tuple[UserNote, ...] = ()
    annotations: tuple[Annotation, ...] = ()
    reflections: tuple[Reflection, ...] = ()

    @property
    def external_identity(self) -> ExternalIdentity:
        return ExternalIdentity(
            source_system=self.source_system,
            external_book_id=self.external_book_id,
        )
