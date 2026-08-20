"""Lossless chapter-content splitting at Markdown block boundaries."""

import re

_FENCE_OPENING = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})")
_TABLE_DELIMITER_CELL = re.compile(r":?-+:?")

def split_chapter_content(content: str, max_characters: int) -> tuple[str, ...]:
    """Greedily pack whole Markdown blocks without truncating atomic blocks."""
    if max_characters < 1:
        raise ValueError("max_characters must be positive")
    if not content:
        return ()

    chunks: list[str] = []
    current = ""
    for block, is_atomic in _blocks(content):
        if is_atomic:
            if current and len(current) + len(block) > max_characters:
                chunks.append(current)
                current = ""
            current += block
            continue

        if len(block) <= max_characters:
            if current and len(current) + len(block) > max_characters:
                chunks.append(current)
                current = ""
            current += block
            continue

        if current:
            chunks.append(current)
            current = ""
        remaining = block
        while remaining:
            if len(remaining) <= max_characters:
                current += remaining
                break
            current += remaining[:max_characters]
            chunks.append(current)
            current = ""
            remaining = remaining[max_characters:]
    if current:
        chunks.append(current)
    return tuple(chunks)


def _blocks(content: str) -> tuple[tuple[str, bool], ...]:
    lines = content.splitlines(keepends=True)
    blocks: list[tuple[str, bool]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _fence_marker(line) is not None:
            block, index = _fenced_block(lines, index)
            is_atomic = True
        elif _is_table_start(lines, index):
            block, index = _table_block(lines, index)
            is_atomic = True
        elif line.lstrip().startswith("#"):
            block, index = _heading_block(lines, index)
            is_atomic = False
        else:
            block, index = _paragraph_block(lines, index)
            is_atomic = False
        blocks.append((block, is_atomic))
    return tuple(blocks)


def _fence_marker(line: str) -> tuple[str, int] | None:
    match = _FENCE_OPENING.match(line)
    if match is None:
        return None
    marker = match.group("marker")
    if marker[0] == "`" and "`" in line[match.end() :].rstrip("\r\n"):
        return None
    return marker[0], len(marker)


def _fenced_block(lines: list[str], index: int) -> tuple[str, int]:
    marker = _fence_marker(lines[index])
    assert marker is not None
    end = index + 1
    while end < len(lines):
        if _is_closing_fence(lines[end], marker):
            end += 1
            break
        end += 1
    return _with_trailing_blank_lines(lines, index, end)


def _table_block(lines: list[str], index: int) -> tuple[str, int]:
    end = index + 2
    while end < len(lines) and _is_table_row(lines[end]):
        end += 1
    return _with_trailing_blank_lines(lines, index, end)


def _heading_block(lines: list[str], index: int) -> tuple[str, int]:
    return _with_trailing_blank_lines(lines, index, index + 1)


def _paragraph_block(lines: list[str], index: int) -> tuple[str, int]:
    end = index + 1
    while end < len(lines) and lines[end].strip():
        if _fence_marker(lines[end]) is not None or _is_table_start(lines, end):
            break
        if lines[end].lstrip().startswith("#"):
            break
        end += 1
    return _with_trailing_blank_lines(lines, index, end)


def _with_trailing_blank_lines(lines: list[str], start: int, end: int) -> tuple[str, int]:
    while end < len(lines) and not lines[end].strip():
        end += 1
    return "".join(lines[start:end]), end


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    header_cells = _table_cells(lines[index])
    delimiter_cells = _table_cells(lines[index + 1])
    return (
        bool(header_cells)
        and len(header_cells) == len(delimiter_cells)
        and all(_TABLE_DELIMITER_CELL.fullmatch(cell) for cell in delimiter_cells)
    )


def _is_table_row(line: str) -> bool:
    return bool(_table_cells(line))


def _table_cells(line: str) -> tuple[str, ...]:
    candidate = line.rstrip("\r\n").strip()
    if candidate.startswith("|"):
        candidate = candidate[1:]
    if candidate.endswith("|"):
        candidate = candidate[:-1]
    if "|" not in candidate:
        return ()
    return tuple(cell.strip() for cell in candidate.split("|"))


def _is_closing_fence(line: str, opener: tuple[str, int]) -> bool:
    candidate = line.rstrip("\r\n")
    indentation = len(candidate) - len(candidate.lstrip(" "))
    if indentation > 3:
        return False

    marker_character, marker_length = opener
    candidate = candidate[indentation:]
    run_length = len(candidate) - len(candidate.lstrip(marker_character))
    return run_length >= marker_length and not candidate[run_length:].strip(" \t")
