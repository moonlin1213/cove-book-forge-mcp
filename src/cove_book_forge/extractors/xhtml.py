from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PageElement, Tag

from cove_book_forge.extractors.sanitize import sanitize_text

_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "footer",
        "header",
        "main",
        "nav",
        "p",
        "section",
    }
)
_REMOVED_TAGS = ("script", "style", "form", "noscript", "svg")
_WHITESPACE = re.compile(r"[\t\r\n ]+")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")
_MAX_DOM_DEPTH = 128


@dataclass(frozen=True, slots=True)
class XhtmlContent:
    heading: str
    content: str


def _clean_inline(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def _ensure_safe_dom_depth(root: Tag) -> None:
    stack: list[tuple[Tag, int]] = [(root, 0)]
    while stack:
        tag, depth = stack.pop()
        if depth > _MAX_DOM_DEPTH:
            raise ValueError("XHTML DOM exceeds the safe depth limit")
        stack.extend((child, depth + 1) for child in tag.children if isinstance(child, Tag))


def _attribute_with_local_name(tag: Tag, local_name: str) -> str:
    for name, value in tag.attrs.items():
        if str(name).split(":")[-1] == local_name and isinstance(value, str):
            return value
    return ""


def _inline_text(node: PageElement) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""
    if node.name == "br":
        return "\n"
    if node.name == "a" and "noteref" in _attribute_with_local_name(node, "type").split():
        label = _clean_inline("".join(node.strings))
        return f"[{label}]" if label else ""
    if node.name == "code" and (node.parent is None or node.parent.name != "pre"):
        value = _clean_inline("".join(node.strings))
        return f"`{value}`" if value else ""
    return "".join(_inline_text(child) for child in node.children)


def _list_item_text(item: Tag) -> str:
    parts: list[str] = []
    for child in item.children:
        if isinstance(child, Tag) and child.name in {"ol", "ul"}:
            continue
        parts.append(_inline_text(child) if isinstance(child, (Tag, NavigableString)) else "")
    return _clean_inline("".join(parts))


def _render_list(tag: Tag, *, depth: int = 0) -> str:
    lines: list[str] = []
    ordered = tag.name == "ol"
    items = tag.find_all("li", recursive=False)
    for index, item in enumerate(items, start=1):
        marker = f"{index}." if ordered else "-"
        text = _list_item_text(item)
        if text:
            lines.append(f"{'  ' * depth}{marker} {text}")
        for nested in item.find_all(["ol", "ul"], recursive=False):
            nested_text = _render_list(nested, depth=depth + 1)
            if nested_text:
                lines.append(nested_text)
    return "\n".join(lines)


def _render_table(tag: Tag) -> str:
    rows: list[str] = []
    for row in tag.find_all("tr"):
        cells = [
            _clean_inline(_inline_text(cell))
            for cell in row.find_all(["th", "td"], recursive=False)
        ]
        if cells:
            rows.append(f"| {' | '.join(cells)} |")
    return "\n".join(rows)


def _render_children(tag: Tag, footnote_labels: dict[str, str]) -> str:
    blocks: list[str] = []
    inline_parts: list[str] = []

    def flush_inline() -> None:
        value = _clean_inline("".join(inline_parts))
        inline_parts.clear()
        if value:
            blocks.append(value)

    for child in tag.children:
        if isinstance(child, NavigableString):
            inline_parts.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        if child.name in _BLOCK_TAGS or child.name in {
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "ol",
            "pre",
            "table",
            "ul",
        }:
            flush_inline()
            rendered = _render_block(child, footnote_labels)
            if rendered:
                blocks.append(rendered)
        else:
            inline_parts.append(_inline_text(child))
    flush_inline()
    return "\n\n".join(blocks)


def _render_block(tag: Tag, footnote_labels: dict[str, str]) -> str:
    if tag.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(tag.name[1])
        value = _clean_inline(_inline_text(tag))
        return f"{'#' * level} {value}" if value else ""
    if tag.name in {"ol", "ul"}:
        return _render_list(tag)
    if tag.name == "pre":
        value = tag.get_text().strip("\n")
        longest_backtick_run = max((len(run) for run in re.findall(r"`+", value)), default=0)
        fence = "`" * max(3, longest_backtick_run + 1)
        return f"{fence}\n{value}\n{fence}" if value.strip() else ""
    if tag.name == "table":
        return _render_table(tag)
    if tag.name == "blockquote":
        value = _render_children(tag, footnote_labels)
        return "\n".join(f"> {line}" if line else ">" for line in value.splitlines())
    if tag.name == "aside" and "footnote" in _attribute_with_local_name(tag, "type").split():
        identifier = str(tag.get("id", ""))
        label = footnote_labels.get(identifier, identifier)
        value = _render_children(tag, footnote_labels)
        return f"[{label}] {value}".strip() if value else ""
    if tag.name == "p":
        return _clean_inline(_inline_text(tag))
    return _render_children(tag, footnote_labels)


def extract_xhtml(payload: bytes) -> XhtmlContent:
    soup = BeautifulSoup(payload, "html.parser")
    _ensure_safe_dom_depth(soup)
    for tag in soup.find_all(_REMOVED_TAGS):
        tag.decompose()

    footnote_labels: dict[str, str] = {}
    for link in soup.find_all("a"):
        if "noteref" not in _attribute_with_local_name(link, "type").split():
            continue
        href = link.get("href")
        if isinstance(href, str) and href.startswith("#"):
            footnote_labels[href[1:]] = _clean_inline("".join(link.strings))

    heading_tag = soup.find(["h1", "h2", "h3", "h4", "h5", "h6"])
    heading = _clean_inline(_inline_text(heading_tag)) if isinstance(heading_tag, Tag) else ""
    body = soup.body
    content = "" if body is None else _render_children(body, footnote_labels)
    content = sanitize_text(_EXCESS_BLANK_LINES.sub("\n\n", content).strip())
    return XhtmlContent(heading=sanitize_text(heading), content=content)
