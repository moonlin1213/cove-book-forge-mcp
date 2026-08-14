from cove_book_forge.extractors.sanitize import sanitize_text


def test_sanitize_text_removes_invisible_and_bidirectional_payload_characters() -> None:
    source = "中文\u200b text\u202e.txt عربي\u2066עברית\u2069"

    assert sanitize_text(source) == "中文 text.txt عربيעברית"


def test_sanitize_text_preserves_ordinary_chinese_arabic_and_hebrew_text() -> None:
    source = "普通话， العربية، עברית."

    assert sanitize_text(source) == source
