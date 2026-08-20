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
