"""Lossless chapter-content splitting at Markdown block boundaries."""


def split_chapter_content(content: str, max_characters: int) -> tuple[str, ...]:
    """Greedily pack whole Markdown blocks without truncating atomic blocks."""
    if max_characters < 1:
        raise ValueError("max_characters must be positive")
    if not content:
        return ()

    chunks: list[str] = []
    current = ""
    for block in _blocks(content):
        if current and len(current) + len(block) > max_characters:
            chunks.append(current)
            current = ""
        current += block
    if current:
        chunks.append(current)
    return tuple(chunks)


def _blocks(content: str) -> tuple[str, ...]:
    lines = content.splitlines(keepends=True)
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _fence_marker(line) is not None:
            block, index = _fenced_block(lines, index)
        elif _is_table_line(line):
            block, index = _table_block(lines, index)
        elif line.lstrip().startswith("#"):
            block, index = _heading_block(lines, index)
        else:
            block, index = _paragraph_block(lines, index)
        blocks.append(block)
    return tuple(blocks)


def _fence_marker(line: str) -> str | None:
    stripped = line.lstrip()
    if stripped.startswith("```"):
        return "```"
    if stripped.startswith("~~~"):
        return "~~~"
    return None


def _fenced_block(lines: list[str], index: int) -> tuple[str, int]:
    marker = _fence_marker(lines[index])
    assert marker is not None
    end = index + 1
    while end < len(lines):
        if lines[end].lstrip().startswith(marker):
            end += 1
            break
        end += 1
    return _with_trailing_blank_lines(lines, index, end)


def _table_block(lines: list[str], index: int) -> tuple[str, int]:
    end = index
    while end < len(lines) and _is_table_line(lines[end]):
        end += 1
    return _with_trailing_blank_lines(lines, index, end)


def _heading_block(lines: list[str], index: int) -> tuple[str, int]:
    return _with_trailing_blank_lines(lines, index, index + 1)


def _paragraph_block(lines: list[str], index: int) -> tuple[str, int]:
    end = index + 1
    while end < len(lines) and lines[end].strip():
        if _fence_marker(lines[end]) is not None or _is_table_line(lines[end]):
            break
        if lines[end].lstrip().startswith("#"):
            break
        end += 1
    return _with_trailing_blank_lines(lines, index, end)


def _with_trailing_blank_lines(lines: list[str], start: int, end: int) -> tuple[str, int]:
    while end < len(lines) and not lines[end].strip():
        end += 1
    return "".join(lines[start:end]), end


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")
