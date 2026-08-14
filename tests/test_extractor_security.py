from pathlib import Path

import pytest

from cove_book_forge.contracts import BookFormat, BookMetadata, ChapterContent, ExtractedBook
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors import EpubExtractor
from cove_book_forge.extractors.base import BookExtractorRegistry
from cove_book_forge.extractors.security import (
    ExtractionLimits,
    SourceSnapshot,
    detect_book_format,
    ensure_source_unchanged,
    fingerprint_source,
    validate_source,
)


def test_fingerprint_source_is_a_lowercase_sha256_digest(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"%PDF-1.7\nsynthetic")

    assert (
        fingerprint_source(source)
        == "5aea7a7a5e33d66d021fd52802ceb64ac5b8f377b2be55fddca8607f093ce3ce"
    )


def test_fingerprint_source_enforces_the_supplied_size_limit_while_streaming(
    tmp_path: Path,
) -> None:
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"%PDF-oversized")

    with pytest.raises(ForgeException) as exc_info:
        fingerprint_source(source, limits=ExtractionLimits(max_source_bytes=5))
    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


def test_magic_detection_requires_a_supported_suffix_and_matching_bytes(tmp_path: Path) -> None:
    pdf = tmp_path / "synthetic.pdf"
    epub = tmp_path / "synthetic.epub"
    renamed_pdf = tmp_path / "renamed.epub"
    pdf.write_bytes(b"%PDF-1.7\nsynthetic")
    epub.write_bytes(b"PK\x03\x04synthetic")
    renamed_pdf.write_bytes(b"%PDF-1.7\nsynthetic")

    assert detect_book_format(pdf) is BookFormat.PDF
    assert detect_book_format(epub) is BookFormat.EPUB
    with pytest.raises(ForgeException, match="Source format is not supported") as exc_info:
        detect_book_format(renamed_pdf)
    assert exc_info.value.code is ForgeErrorCode.UNSUPPORTED_FORMAT


def test_validate_source_maps_missing_nonregular_and_oversized_inputs_to_safe_codes(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.pdf"
    directory = tmp_path / "synthetic.pdf"
    oversized = tmp_path / "oversized.pdf"
    directory.mkdir()
    oversized.write_bytes(b"%PDF-12345")

    for source in (missing, directory):
        with pytest.raises(ForgeException) as exc_info:
            validate_source(source)
        assert exc_info.value.code is ForgeErrorCode.SOURCE_NOT_FOUND
        assert str(source) not in str(exc_info.value)

    with pytest.raises(ForgeException) as exc_info:
        validate_source(oversized, limits=ExtractionLimits(max_source_bytes=5))
    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED
    assert str(oversized) not in str(exc_info.value)


def test_source_snapshot_detects_a_change_after_parsing(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"%PDF-1.7\nfirst")
    snapshot = SourceSnapshot.capture(source)
    source.write_bytes(b"%PDF-1.7\nsecond")

    with pytest.raises(ForgeException) as exc_info:
        ensure_source_unchanged(source, snapshot)
    assert exc_info.value.code is ForgeErrorCode.SOURCE_CHANGED


def test_post_snapshot_validation_failures_map_to_source_changed(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"%PDF-1.7\nfirst")
    snapshot = SourceSnapshot.capture(source)
    source.unlink()

    with pytest.raises(ForgeException) as exc_info:
        ensure_source_unchanged(source, snapshot)
    assert exc_info.value.code is ForgeErrorCode.SOURCE_CHANGED
    assert str(source) not in str(exc_info.value)


def test_registry_dispatches_only_the_detected_format(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"%PDF-1.7\nsynthetic")
    registry = BookExtractorRegistry({BookFormat.EPUB: EpubExtractor()})

    assert registry.get_for_source(source) is None


def test_registry_rejects_an_extractor_result_with_the_wrong_fingerprint(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"%PDF-1.7\nsynthetic")

    class IncorrectFingerprintExtractor:
        def extract(self, source: Path, fingerprint: str) -> ExtractedBook:
            return ExtractedBook(
                format=BookFormat.PDF,
                metadata=BookMetadata(title="Synthetic book"),
                chapters=(ChapterContent(index=0, content="Synthetic content."),),
                source_fingerprint="f" * 64,
            )

    registry = BookExtractorRegistry({BookFormat.PDF: IncorrectFingerprintExtractor()})

    with pytest.raises(ForgeException) as exc_info:
        registry.extract(source)
    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED


def test_registry_applies_its_injected_source_limit_before_dispatch(tmp_path: Path) -> None:
    source = tmp_path / "synthetic.pdf"
    source.write_bytes(b"%PDF-oversized")
    registry = BookExtractorRegistry(limits=ExtractionLimits(max_source_bytes=5))

    with pytest.raises(ForgeException) as exc_info:
        registry.extract(source)
    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED
