from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_URL = "https://github.com/virgiliojr94/book-to-skill"


def test_readme_and_acknowledgements_credit_book_to_skill() -> None:
    for name in ("README.md", "ACKNOWLEDGEMENTS.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "Virgilio Jr." in text
        assert UPSTREAM_URL in text


def test_third_party_notice_preserves_upstream_copyright() -> None:
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "Copyright (c) 2025 virgiliojr94" in text
    assert "MIT License" in text
