from pathlib import Path

from fixtures import opf_document, write_epub, write_pdf, xhtml_document

from cove_book_forge.config import AppConfig
from cove_book_forge.contracts import (
    Annotation,
    BookMetadata,
    BookRef,
    ChapterContent,
    ChapterSnapshot,
    Highlight,
    ImportMode,
    Reflection,
    UserNote,
)
from cove_book_forge.library import create_book_library


def _config(data_dir: Path, *, enabled: bool = True) -> AppConfig:
    return AppConfig.model_validate(
        {
            "library": {"enabled": enabled, "data_dir": data_dir},
            "model": {"provider": "test", "model": "test"},
        }
    )


def _ordered_epub(path: Path) -> Path:
    opf = opf_document(
        title="Runtime EPUB",
        author="Fixture Author",
        manifest="""
          <item id="later" href="text/02-later.xhtml" media-type="application/xhtml+xml"/>
          <item id="earlier" href="text/01-earlier.xhtml" media-type="application/xhtml+xml"/>
          <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
        """,
        spine='<itemref idref="later"/><itemref idref="earlier"/>',
    )
    nav = xhtml_document("""
      <nav epub:type="toc"><ol>
        <li><a href="text/01-earlier.xhtml">Navigation Earlier</a></li>
        <li><a href="text/02-later.xhtml">Navigation Later</a></li>
      </ol></nav>
    """)
    return write_epub(
        path,
        opf=opf,
        members={
            "OEBPS/nav.xhtml": nav,
            "OEBPS/text/01-earlier.xhtml": xhtml_document("<p>Filename-first body.</p>"),
            "OEBPS/text/02-later.xhtml": xhtml_document("<p>Spine-first body.</p>"),
        },
    )


def test_default_epub_copy_import_survives_restart_in_spine_order(tmp_path: Path) -> None:
    data_dir = tmp_path / "library"
    source = _ordered_epub(tmp_path / "runtime.epub")
    imported = create_book_library(_config(data_dir)).import_book(source, ImportMode.COPY)

    restarted = create_book_library(_config(data_dir))

    assert restarted.list_books() == (restarted.get_book(imported.book),)
    assert restarted.get_book(imported.book).source_available is True
    assert restarted.get_chapter(imported.book, 0) == ChapterContent(
        index=0,
        title="Navigation Later",
        content="Spine-first body.",
        source_locator="epub:OEBPS/text/02-later.xhtml",
    )
    assert restarted.get_chapter(imported.book, 1).content == "Filename-first body."


def test_default_pdf_reference_keeps_chapters_after_source_change_and_loss(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "library"
    source = write_pdf(
        tmp_path / "runtime.pdf",
        pages=[[(700, "First page text.")], [(700, "Second page text.")]],
        metadata={"Title": "Runtime PDF", "Author": "Fixture Author"},
    )
    original = source.read_bytes()
    imported = create_book_library(_config(data_dir)).import_book(source, ImportMode.REFERENCE)
    restarted = create_book_library(_config(data_dir))

    chapter = restarted.get_chapter(imported.book, 0)
    assert "First page text." in chapter.content
    assert "Second page text." in chapter.content

    source.write_bytes(original + b"\n% source changed")
    assert restarted.get_book(imported.book).source_available is False
    assert restarted.get_chapter(imported.book, 0) == chapter

    source.write_bytes(original)
    source.unlink()
    assert restarted.get_book(imported.book).source_available is False
    assert restarted.get_chapter(imported.book, 0) == chapter


def test_external_snapshot_round_trip_survives_restart_when_library_is_disabled(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "external-cache"
    snapshot = ChapterSnapshot(
        source_system="reader",
        external_book_id="stable-external-book",
        book=BookMetadata(
            title="External Runtime Book",
            author="External Author",
            language="en",
            total_chapters=1,
        ),
        chapter=ChapterContent(
            index=0,
            title="External Chapter",
            content="Complete normalized chapter content.",
            source_locator="reader:chapter:0",
        ),
        highlights=(Highlight(id="highlight-1", text="normalized", paragraph_index=0),),
        user_notes=(UserNote(id="note-1", text="Keep this.", paragraph_index=0),),
        annotations=(Annotation(id="annotation-1", text="Context", author_label="Editor"),),
        reflections=(Reflection(id="reflection-1", text="Reflection", author_label="Reader"),),
    )
    book = create_book_library(_config(data_dir, enabled=False)).upsert_chapter_snapshot(snapshot)

    restarted = create_book_library(_config(data_dir, enabled=False))

    assert restarted.list_books() == (restarted.get_book(book),)
    assert restarted.get_book(book).book == BookRef(book_id=book.book_id)
    assert restarted.get_book(book).metadata == snapshot.book
    assert restarted.get_chapter(book, 0) == snapshot.chapter
