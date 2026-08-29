from pathlib import Path

import pytest

from cove_book_forge.contracts import BookFormat
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.extractors import BookExtractorRegistry


def test_default_registry_extracts_utf8_txt_as_one_normalized_chapter(tmp_path: Path) -> None:
    source = tmp_path / "月亮与六便士.txt"
    source.write_text(
        "\ufeff第一章\r\n你好\u202e世界\r\n\r\n第二段\n",
        encoding="utf-8",
    )

    extracted = BookExtractorRegistry().extract(source)

    assert extracted.format is BookFormat.TXT
    assert extracted.metadata.title == "月亮与六便士"
    assert extracted.metadata.total_chapters == 1
    assert extracted.chapters[0].title == "月亮与六便士"
    assert extracted.chapters[0].content == "第一章\n你好世界\n\n第二段"
    assert extracted.chapters[0].source_locator == "txt:document"


def test_default_registry_extracts_bom_tagged_utf16_txt(tmp_path: Path) -> None:
    source = tmp_path / "legacy.TXT"
    source.write_text("Legacy text.\r\n第二行。", encoding="utf-16")

    extracted = BookExtractorRegistry().extract(source)

    assert extracted.format is BookFormat.TXT
    assert extracted.chapters[0].content == "Legacy text.\n第二行。"


@pytest.mark.parametrize(
    "payload",
    [
        b"\x00binary-looking-text",
        b"\xff\xfeinvalid-without-complete-code-unit\x00",
        b"  \r\n\t  ",
    ],
)
def test_txt_rejects_binary_malformed_and_empty_content(
    tmp_path: Path,
    payload: bytes,
) -> None:
    source = tmp_path / "unsafe.txt"
    source.write_bytes(payload)

    with pytest.raises(ForgeException) as exc_info:
        BookExtractorRegistry().extract(source)

    assert exc_info.value.code is ForgeErrorCode.EXTRACTION_FAILED
    assert str(source) not in str(exc_info.value)
