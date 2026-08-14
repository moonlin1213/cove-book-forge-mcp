from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixtures import write_pdf

from cove_book_forge.contracts import BookFormat, ExtractedBook, PdfProfile
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors import LayoutPdfExtractor, PdfExtractor
from cove_book_forge.extractors.base import BookExtractorRegistry
from cove_book_forge.extractors.security import ExtractionLimits

FINGERPRINT = "d" * 64


def _body(page_number: int) -> list[tuple[float, str]]:
    return [
        (760, "Repeated Book Header"),
        (620, f"Main text from physical page {page_number}."),
        (400, "Repeated Book Header"),
        (60, "Repeated Book Footer"),
        (40, str(page_number)),
    ]


def test_pdf_preserves_metadata_page_order_and_supplied_fingerprint(tmp_path: Path) -> None:
    source = write_pdf(
        tmp_path / "ordered.pdf",
        pages=[[(700, "First page prose.")], [(700, "Second page prose.")]],
        metadata={"Title": "Synthetic PDF", "Author": "Fixture Author"},
    )

    extracted = PdfExtractor().extract(source, FINGERPRINT)

    assert extracted.format is BookFormat.PDF
    assert extracted.metadata.title == "Synthetic PDF"
    assert extracted.metadata.author == "Fixture Author"
    assert extracted.metadata.total_chapters == 1
    assert extracted.source_fingerprint == FINGERPRINT
    assert extracted.pdf_profile is PdfProfile.TEXT
    assert extracted.chapters[0].content.index("First page prose.") < extracted.chapters[
        0
    ].content.index("Second page prose.")


def test_pdf_uses_filename_and_empty_author_when_metadata_is_missing(tmp_path: Path) -> None:
    source = write_pdf(
        tmp_path / "fallback-title.pdf",
        pages=[[(700, "A genuinely short note.")]],
    )

    extracted = PdfExtractor().extract(source, FINGERPRINT)

    assert extracted.metadata.title == "fallback-title"
    assert extracted.metadata.author == ""


def test_pdf_removes_only_repeated_edge_boilerplate_and_page_numbers(tmp_path: Path) -> None:
    source = write_pdf(tmp_path / "edges.pdf", pages=[_body(1), _body(2), _body(3)])

    content = PdfExtractor().extract(source, FINGERPRINT).chapters[0].content

    assert content.count("Repeated Book Header") == 3
    assert "Repeated Book Footer" not in content
    assert "Main text from physical page 1." in content
    assert "Main text from physical page 2." in content
    assert "Main text from physical page 3." in content
    assert "\n1\n" not in f"\n{content}\n"
    assert "\n2\n" not in f"\n{content}\n"
    assert "\n3\n" not in f"\n{content}\n"


def test_pdf_rejects_encrypted_documents_before_text_extraction(tmp_path: Path) -> None:
    source = write_pdf(
        tmp_path / "secret.pdf",
        pages=[[(700, "Private body text.")]],
        password="secret",
    )

    with pytest.raises(ForgeException) as exc_info:
        PdfExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.ENCRYPTED_DOCUMENT
    assert str(source) not in str(exc_info.value)
    assert "Private body text" not in str(exc_info.value)


def test_pdf_maps_corruption_to_a_safe_public_error(tmp_path: Path) -> None:
    source = tmp_path / "private-corrupt.pdf"
    source.write_bytes(b"%PDF-1.7\nprivate body marker\nnot a valid cross-reference")

    with pytest.raises(ForgeException) as exc_info:
        PdfExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED
    public_payload = json.dumps(exc_info.value.as_result())
    assert str(source) not in public_payload
    assert "private body marker" not in public_payload
    assert "cross-reference" not in public_payload


def test_pdf_enforces_the_injected_page_limit(tmp_path: Path) -> None:
    source = write_pdf(
        tmp_path / "too-many-pages.pdf",
        pages=[[(700, "First page.")], [(700, "Second page.")]],
    )

    with pytest.raises(ForgeException) as exc_info:
        PdfExtractor(limits=ExtractionLimits(max_pdf_pages=1)).extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


@pytest.mark.parametrize(
    "pages",
    [
        [[]],
        [[(40, "1")], [(40, "2")]],
    ],
)
def test_pdf_requires_ocr_without_a_meaningful_text_layer(
    tmp_path: Path, pages: list[list[tuple[float, str]]]
) -> None:
    source = write_pdf(tmp_path / "scan.pdf", pages=pages)

    with pytest.raises(ForgeException) as exc_info:
        PdfExtractor().extract(source, FINGERPRINT)

    assert exc_info.value.code is ForgeErrorCode.OCR_REQUIRED


def test_pdf_accepts_a_genuinely_short_text_layer(tmp_path: Path) -> None:
    source = write_pdf(tmp_path / "short.pdf", pages=[[(700, "Short note.")]])

    extracted = PdfExtractor().extract(source, FINGERPRINT)

    assert extracted.chapters[0].content == "Short note."


def test_pdf_marks_structurally_technical_content(tmp_path: Path) -> None:
    source = write_pdf(
        tmp_path / "technical.pdf",
        pages=[
            [
                (700, "def parse(value):"),
                (680, "    return value + 1"),
                (660, "Column | Value | Total"),
                (640, "alpha = beta + gamma"),
            ]
        ],
    )

    extracted = PdfExtractor().extract(source, FINGERPRINT)

    assert extracted.pdf_profile is PdfProfile.TECHNICAL
    assert "    return value + 1" in extracted.chapters[0].content


def test_pdf_uses_valid_increasing_outline_destinations_as_ranges(tmp_path: Path) -> None:
    source = write_pdf(
        tmp_path / "outlined.pdf",
        pages=[[(700, f"Readable content page {index}.")] for index in range(1, 6)],
        outline=[
            ("Opening", 0),
            ("Duplicate", 0),
            ("Part Two", 2),
            ("Out of order", 1),
            ("Invalid", None),
        ],
    )

    extracted = PdfExtractor().extract(source, FINGERPRINT)

    assert [chapter.index for chapter in extracted.chapters] == [0, 1]
    assert [chapter.title for chapter in extracted.chapters] == ["Opening", "Part Two"]
    assert [chapter.source_locator for chapter in extracted.chapters] == [
        "pdf:pages:1-2",
        "pdf:pages:3-5",
    ]
    assert "Readable content page 2." in extracted.chapters[0].content
    assert "Readable content page 3." in extracted.chapters[1].content


def test_pdf_fallback_groups_twenty_physical_pages_and_preserves_all_text(
    tmp_path: Path,
) -> None:
    source = write_pdf(
        tmp_path / "fallback-ranges.pdf",
        pages=[[(700, f"Physical page {index} readable text.")] for index in range(1, 23)],
    )

    extracted = PdfExtractor().extract(source, FINGERPRINT)

    assert [chapter.index for chapter in extracted.chapters] == [0, 1]
    assert [chapter.source_locator for chapter in extracted.chapters] == [
        "pdf:pages:1-20",
        "pdf:pages:21-22",
    ]
    assert "Physical page 20 readable text." in extracted.chapters[0].content
    assert "Physical page 21 readable text." not in extracted.chapters[0].content
    assert "Physical page 21 readable text." in extracted.chapters[1].content
    assert "Physical page 22 readable text." in extracted.chapters[1].content


def test_default_registry_registers_pdf_without_changing_explicit_mappings(
    tmp_path: Path,
) -> None:
    source = write_pdf(tmp_path / "registry.pdf", pages=[[(700, "Registry body text.")]])
    default_extractor = BookExtractorRegistry().get_for_source(source)

    class ExplicitExtractor:
        def extract(self, source: Path, fingerprint: str) -> ExtractedBook:
            raise AssertionError("not called")

    explicit = ExplicitExtractor()

    assert isinstance(default_extractor, PdfExtractor)
    assert BookExtractorRegistry({BookFormat.PDF: explicit}).get_for_source(source) is explicit
    assert LayoutPdfExtractor is not None
