import pytest

from cove_book_forge.analysis.chunks import split_chapter_content


def test_splitter_packs_heading_and_paragraph_blocks_without_losing_or_reordering_text() -> None:
    content = "# First\n\nalpha\n\n## Second\n\nbeta\n\n### Third\n\ngamma"

    chunks = split_chapter_content(content, max_characters=20)

    assert chunks == ("# First\n\nalpha\n\n", "## Second\n\nbeta\n\n", "### Third\n\ngamma")
    assert "".join(chunks) == content


def test_splitter_keeps_fenced_code_and_contiguous_markdown_table_atomic() -> None:
    content = (
        "Before\n\n"
        "```python\nprint('not split')\n```\n\n"
        "| name | value |\n| --- | --- |\n| a | 1 |\n| b | 2 |\n\n"
        "After"
    )

    chunks = split_chapter_content(content, max_characters=18)

    assert chunks == (
        "Before\n\n",
        "```python\nprint('not split')\n```\n\n",
        "| name | value |\n| --- | --- |\n| a | 1 |\n| b | 2 |\n\n",
        "After",
    )
    assert "".join(chunks) == content


def test_splitter_leaves_an_oversized_atomic_block_whole() -> None:
    content = "```text\n" + ("unbroken\n" * 20) + "```"

    chunks = split_chapter_content(content, max_characters=32)

    assert chunks == (content,)


def test_splitter_returns_the_original_content_as_one_chunk_when_it_fits() -> None:
    content = "第一段\n\n第二段"

    assert split_chapter_content(content, max_characters=128) == (content,)


def test_splitter_splits_an_oversized_non_atomic_paragraph_at_lossless_character_boundaries() -> None:
    content = "abcdefghijklmnopqrstuvwxyz"

    chunks = split_chapter_content(content, max_characters=7)

    assert chunks == ("abcdefg", "hijklmn", "opqrstu", "vwxyz")
    assert "".join(chunks) == content
    assert all(len(chunk) <= 7 for chunk in chunks)


@pytest.mark.parametrize("fence", ["`", "~"])
def test_splitter_keeps_four_character_fences_open_until_a_matching_closer(
    fence: str,
) -> None:
    marker = fence * 4
    content = f"Before\n\n{marker}python\ninside\n{fence * 3}\nstill code\n{marker}\n\nAfter"

    chunks = split_chapter_content(content, max_characters=18)

    assert chunks == ("Before\n\n", f"{marker}python\ninside\n{fence * 3}\nstill code\n{marker}\n\n", "After")
    assert "".join(chunks) == content


def test_splitter_does_not_treat_fence_text_with_trailing_content_as_a_closer() -> None:
    content = "```python\ninside\n``` not a closer\nstill code\n```\n\nAfter"

    chunks = split_chapter_content(content, max_characters=18)

    assert chunks == ("```python\ninside\n``` not a closer\nstill code\n```\n\n", "After")
    assert "".join(chunks) == content


def test_splitter_accepts_a_legally_indented_matching_fence_closer() -> None:
    content = "  ```python\n  inside\n  ```\n\nAfter"

    assert split_chapter_content(content, max_characters=18) == (
        "  ```python\n  inside\n  ```\n\n",
        "After",
    )
