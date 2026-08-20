"""Deterministic, filesystem-free rendering for managed Obsidian Markdown."""

from __future__ import annotations

import hashlib
import html
import json
import posixpath
import re
import unicodedata
from collections.abc import Iterable
from typing import Any, Literal
from urllib.parse import quote

from cove_book_forge.config.models import ObsidianOutputConfig
from cove_book_forge.contracts.analysis import (
    AnalyzedChapter,
    ChapterAnalysis,
    Concept,
    DecisionRule,
)
from cove_book_forge.contracts.books import ChapterSnapshot
from cove_book_forge.outputs.obsidian_models import (
    ObsidianBookManifest,
    ObsidianCardManifest,
    ObsidianChapterManifest,
    RenderedObsidianBook,
)
from cove_book_forge.path_safety import (
    has_reserved_stem,
    validate_component,
    validate_relative_path,
)

_SCHEMA: Literal[1] = 1
_MARKDOWN_FIELDS = (
    "cove_book_forge",
    "cove_schema",
    "cove_kind",
    "cove_book_key",
    "cove_chapter_index",
    "cove_source_fingerprint",
    "cove_stable_id",
    "cove_body_sha256",
)
_UNSAFE_FILENAME = re.compile(r'[\\/:*?"<>|%]+')
_BARE_LINK = re.compile(r"(?i)\b(?:www\.|[a-z][a-z0-9+.-]*:)(?=\S)")
_EMAIL_LINK = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}(?![\w.-])")
_BLOCK_LINE = re.compile(r"^ {0,3}(?:#{1,6}(?:\s|$)|>|[-+*]\s|\d+[.)]\s|`{3,}|~{3,}|(?:=+|-+)\s*$)")
_MAX_MISSING_CHAPTER_PREVIEW = 100


def canonical_manifest_bytes(manifest: ObsidianBookManifest) -> bytes:
    """Serialize a manifest as canonical UTF-8 JSON, including its checksum."""
    return _canonical_json_bytes(manifest.model_dump(mode="json", by_alias=True))


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _normalized_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _book_key(snapshot: ChapterSnapshot) -> str:
    identity = [
        _normalized_text(snapshot.source_system),
        _normalized_text(snapshot.external_book_id),
    ]
    return hashlib.sha256(_canonical_json_bytes(identity)).hexdigest()[:16]


def _safe_component(value: str, fallback: str, *, limit: int = 80) -> str:
    normalized = _normalized_text(value)
    original = normalized
    normalized = " ".join(normalized.split())
    normalized = _UNSAFE_FILENAME.sub("-", normalized)
    normalized = "".join(
        character for character in normalized if not unicodedata.category(character).startswith("C")
    )
    normalized = normalized.strip(" .-")
    if normalized in {"", ".", ".."}:
        normalized = fallback
    if has_reserved_stem(normalized):
        normalized = f"{fallback}-{normalized}"

    def clip_bytes(text: str, budget: int) -> str:
        result = ""
        for character in text:
            if len((result + character).encode("utf-8")) > budget:
                break
            result += character
        return result

    truncated = clip_bytes(normalized, limit).rstrip(" .") or fallback
    if truncated != original:
        suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
        truncated = f"{clip_bytes(truncated, limit - len(suffix) - 2).rstrip(' .')}--{suffix}"
    return validate_component(truncated, max_bytes=limit)


def _markdown_text(value: str) -> str:
    """Render untrusted content as inert Markdown data, preserving visible text."""
    escaped = html.escape(_normalized_text(value), quote=False)
    escaped = escaped.replace("\\", "\\\\")
    escaped = _BARE_LINK.sub(
        lambda match: match.group(0).replace(":", "\\:").replace(".", "\\."), escaped
    )
    escaped = _EMAIL_LINK.sub(lambda match: match.group(0).replace("@", "\\@"), escaped)
    for character in ("`", "*", "_", "[", "]"):
        escaped = escaped.replace(character, f"\\{character}")
    lines = escaped.split("\n")
    return "\n".join(f"\\{line}" if _BLOCK_LINE.match(line) else line for line in lines)


def _markdown_link(label: str, source_path: str, target_path: str) -> str:
    relative = posixpath.relpath(target_path, start=posixpath.dirname(source_path) or ".")
    validate_relative_path(target_path)
    return f"[{_markdown_text(label)}]({quote(relative, safe='/-._~')})"


def _list(items: Iterable[str]) -> list[str]:
    values = [item for item in items if item]
    return [f"- {_markdown_text(value)}" for value in values] or ["- None."]


def _frontmatter(
    *,
    kind: str,
    book_key: str,
    chapter_index: int,
    fingerprint: str,
    stable_id: str,
    body: str,
) -> bytes:
    body_bytes = _normalized_text(body).encode("utf-8")
    values: dict[str, str] = {
        "cove_book_forge": "managed",
        "cove_schema": str(_SCHEMA),
        "cove_kind": kind,
        "cove_book_key": book_key,
        "cove_chapter_index": str(chapter_index),
        "cove_source_fingerprint": fingerprint,
        "cove_stable_id": stable_id,
        "cove_body_sha256": hashlib.sha256(body_bytes).hexdigest(),
    }
    assert tuple(values) == _MARKDOWN_FIELDS
    header = (
        "---\n"
        + "".join(
            f"{field}: {json.dumps(values[field], ensure_ascii=False, separators=(',', ':'))}\n"
            for field in _MARKDOWN_FIELDS
        )
        + "---\n"
    )
    return header.encode("utf-8") + body_bytes


def _card_content(
    kind: Literal["concept", "decision_rule"], item: Concept | DecisionRule
) -> dict[str, Any]:
    if kind == "concept":
        concept = item
        assert isinstance(concept, Concept)
        return {
            "term": _normalized_text(concept.term),
            "definition": _normalized_text(concept.definition),
            "evidence_refs": [
                {
                    "locator": _normalized_text(reference.locator),
                    "note": _normalized_text(reference.note),
                }
                for reference in concept.evidence_refs
            ],
        }
    rule = item
    assert isinstance(rule, DecisionRule)
    return {
        "rule": _normalized_text(rule.rule),
        "conditions": [_normalized_text(condition) for condition in rule.conditions],
        "evidence_refs": [
            {
                "locator": _normalized_text(reference.locator),
                "note": _normalized_text(reference.note),
            }
            for reference in rule.evidence_refs
        ],
    }


def _stable_card_id(
    *,
    book_key: str,
    chapter_index: int,
    kind: Literal["concept", "decision_rule"],
    content: dict[str, Any],
    ordinal: int,
) -> str:
    payload = {
        "book_key": book_key,
        "chapter_index": chapter_index,
        "kind": kind,
        "content": content,
        "ordinal": ordinal,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()[:16]


def _manifest_checksum(manifest: ObsidianBookManifest) -> str:
    payload = manifest.model_dump(mode="json", by_alias=True, exclude={"checksum"})
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


class ObsidianRenderer:
    """Render one validated analysis without reading, writing, or parsing files."""

    def __init__(self, config: ObsidianOutputConfig) -> None:
        self._config = config

    def render(
        self,
        snapshot: ChapterSnapshot,
        analyzed: AnalyzedChapter,
        previous: ObsidianBookManifest | None,
    ) -> RenderedObsidianBook:
        book_key = _book_key(snapshot)
        book_title = _safe_component(snapshot.book.title, "Untitled Book", limit=45)
        chapter_title = _safe_component(snapshot.chapter.title, "Untitled Chapter", limit=38)
        candidate_book_dir = f"{self._config.notes_folder}/{book_title}--{book_key}"
        if previous is not None and previous.book_key == book_key:
            book_dir = previous.book_directory
            moc_path = previous.moc_path
        else:
            book_dir = candidate_book_dir
            moc_path = f"{book_dir}/{book_title} MOC.md"
        chapter_path = f"{book_dir}/Chapters/{snapshot.chapter.index + 1:02d} {chapter_title}.md"
        analysis = analyzed.analysis

        card_paths, card_files, cards = self._render_cards(
            snapshot=snapshot,
            analyzed=analyzed,
            book_key=book_key,
            chapter_path=chapter_path,
            moc_path=moc_path,
        )
        chapter_body = self._chapter_body(snapshot, analysis, chapter_path, card_paths, cards)
        chapter_file = _frontmatter(
            kind="chapter",
            book_key=book_key,
            chapter_index=snapshot.chapter.index,
            fingerprint=analyzed.input_fingerprint,
            stable_id=f"{book_key}-{snapshot.chapter.index:04d}",
            body=chapter_body,
        )
        chapter_summary = ObsidianChapterManifest(
            index=snapshot.chapter.index,
            title=chapter_title,
            input_fingerprint=analyzed.input_fingerprint,
            note_path=chapter_path,
            card_paths=card_paths,
            frameworks=tuple(_normalized_text(item.name) for item in analysis.frameworks),
            topics=tuple(_normalized_text(item) for item in analysis.topic_tags),
        )
        manifest = self._build_manifest(
            previous=previous,
            book_key=book_key,
            book_title=book_title,
            total_chapters=snapshot.book.total_chapters,
            moc_path=moc_path,
            current=chapter_summary,
            cards=cards,
        )
        moc_body = self._moc_body(manifest, moc_path)
        moc_file = _frontmatter(
            kind="moc",
            book_key=book_key,
            chapter_index=-1,
            fingerprint=manifest.checksum,
            stable_id=f"{book_key}-moc",
            body=moc_body,
        )
        manifest_path = f".cove-book-forge/obsidian/{book_key}.json"
        files: dict[str, bytes] = {
            chapter_path: chapter_file,
            moc_path: moc_file,
            manifest_path: canonical_manifest_bytes(manifest),
            **card_files,
        }
        for path in files:
            validate_relative_path(path)
        return RenderedObsidianBook(
            files=files,
            manifest=manifest,
            chapter_path=chapter_path,
            moc_path=moc_path,
            card_paths=card_paths,
        )

    def _render_cards(
        self,
        *,
        snapshot: ChapterSnapshot,
        analyzed: AnalyzedChapter,
        book_key: str,
        chapter_path: str,
        moc_path: str,
    ) -> tuple[tuple[str, ...], dict[str, bytes], tuple[ObsidianCardManifest, ...]]:
        seen: dict[tuple[str, str], int] = {}
        paths: list[str] = []
        files: dict[str, bytes] = {}
        summaries: list[ObsidianCardManifest] = []
        entries: list[tuple[Literal["concept", "decision_rule"], Concept | DecisionRule]] = [
            *(("concept", concept) for concept in analyzed.analysis.concepts),
            *(("decision_rule", rule) for rule in analyzed.analysis.decision_rules),
        ]
        for kind, item in entries:
            content = _card_content(kind, item)
            subject = content["term"] if kind == "concept" else content["rule"]
            content_key = (kind, subject)
            ordinal = seen.get(content_key, 0) + 1
            seen[content_key] = ordinal
            stable_id = _stable_card_id(
                book_key=book_key,
                chapter_index=snapshot.chapter.index,
                kind=kind,
                content=content,
                ordinal=ordinal,
            )
            title = _safe_component(
                item.term if isinstance(item, Concept) else item.rule,
                "Concept" if kind == "concept" else "Decision Rule",
            )
            path = f"{self._config.cards_folder}/{title}--{stable_id}.md"
            body = self._card_body(kind, item, path, chapter_path, moc_path)
            files[path] = _frontmatter(
                kind="card",
                book_key=book_key,
                chapter_index=snapshot.chapter.index,
                fingerprint=analyzed.input_fingerprint,
                stable_id=stable_id,
                body=body,
            )
            paths.append(path)
            summaries.append(
                ObsidianCardManifest(
                    stable_id=stable_id,
                    kind=kind,
                    title=title,
                    path=path,
                    chapter_index=snapshot.chapter.index,
                )
            )
        return tuple(paths), files, tuple(summaries)

    def _card_body(
        self,
        kind: Literal["concept", "decision_rule"],
        item: Concept | DecisionRule,
        path: str,
        chapter_path: str,
        moc_path: str,
    ) -> str:
        if kind == "concept":
            concept = item
            assert isinstance(concept, Concept)
            details = ["## Concept", _markdown_text(concept.definition), "", "## Evidence"]
            details.extend(
                _list(f"{ref.locator}: {ref.note}".rstrip(": ") for ref in concept.evidence_refs)
            )
            title = concept.term
        else:
            rule = item
            assert isinstance(rule, DecisionRule)
            details = ["## Decision rule", _markdown_text(rule.rule), "", "## Conditions"]
            details.extend(_list(rule.conditions))
            details.extend(["", "## Evidence"])
            details.extend(
                _list(f"{ref.locator}: {ref.note}".rstrip(": ") for ref in rule.evidence_refs)
            )
            title = rule.rule
        return "\n".join(
            [
                f"# {_markdown_text(title)}",
                "",
                *details,
                "",
                "## Source chapter",
                _markdown_link("Chapter note", path, chapter_path),
                "",
                "## Book MOC",
                _markdown_link("Book MOC", path, moc_path),
                "",
            ]
        )

    def _chapter_body(
        self,
        snapshot: ChapterSnapshot,
        analysis: ChapterAnalysis,
        chapter_path: str,
        card_paths: tuple[str, ...],
        cards: tuple[ObsidianCardManifest, ...],
    ) -> str:
        sections: list[str] = [
            f"# {_markdown_text(snapshot.chapter.title or 'Untitled Chapter')}",
            "",
            "## Source",
        ]
        sections.extend(
            _list(
                (
                    f"Book: {snapshot.book.title}",
                    f"Author: {snapshot.book.author}" if snapshot.book.author else "",
                    f"Source: {snapshot.source_system}",
                    f"Locator: {snapshot.chapter.source_locator}"
                    if snapshot.chapter.source_locator
                    else "",
                )
            )
        )
        sections.extend(
            ["", "## Core idea", _markdown_text(analysis.core_idea), "", "## Frameworks"]
        )
        sections.extend(
            _list(
                f"{item.name}: {item.when_to_use} {'; '.join(item.how)} {item.why} "
                f"limitations: {'; '.join(item.limitations)}".strip()
                for item in analysis.frameworks
            )
        )
        sections.extend(["", "## Concepts"])
        sections.extend(_list(f"{item.term}: {item.definition}" for item in analysis.concepts))
        sections.extend(["", "## Mental models"])
        sections.extend(
            _list(
                f"{item.name}: {item.explanation} {item.when_to_use}"
                for item in analysis.mental_models
            )
        )
        sections.extend(["", "## Methods"])
        sections.extend(
            _list(
                f"{item.name}: {'; '.join(item.steps)} {item.when_to_use} limitations: {'; '.join(item.limitations)}".rstrip(
                    ": "
                )
                for item in analysis.methods
            )
        )
        sections.extend(["", "## Anti-patterns"])
        sections.extend(
            _list(
                f"{item.name}: {item.why} Alternative: {item.alternative}"
                for item in analysis.anti_patterns
            )
        )
        sections.extend(["", "## Decision rules"])
        sections.extend(_list(item.rule for item in analysis.decision_rules))
        sections.extend(["", "## Worked examples"])
        sections.extend(
            _list(
                f"{item.title}: {item.situation} {item.application} {item.result}".strip()
                for item in analysis.worked_examples
            )
        )
        sections.extend(["", "## Key takeaways"])
        sections.extend(_list(analysis.key_takeaways))
        sections.extend(["", "## Highlights"])
        sections.extend(
            _list(
                (
                    *analysis.highlight_insights,
                    *(
                        f"{item.text} {item.note}"
                        f"{' page ' + str(item.page) if item.page is not None else ''}"
                        f"{' paragraph ' + str(item.paragraph_index) if item.paragraph_index is not None else ''}"
                        for item in snapshot.highlights
                    ),
                )
            )
        )
        sections.extend(["", "## User notes"])
        sections.extend(_list(item.text for item in snapshot.user_notes))
        sections.extend(["", "## Annotations and reflections"])
        sections.extend(
            _list(
                (
                    *(
                        f"{item.author_label or 'Annotation'} ({item.kind}"
                        f"{', paragraph ' + str(item.paragraph_index) if item.paragraph_index is not None else ''}): {item.text}"
                        for item in snapshot.annotations
                    ),
                    *(
                        f"{item.author_label or 'Reflection'} (reflection): {item.text}"
                        for item in snapshot.reflections
                    ),
                    *analysis.annotation_insights,
                )
            )
        )
        sections.extend(["", "## Evidence"])
        sections.extend(
            _list(f"{item.locator}: {item.note}".rstrip(": ") for item in analysis.evidence_refs)
        )
        sections.extend(["", "## Quality warnings"])
        sections.extend(_list(f"{item.code}: {item.message}" for item in analysis.quality_warnings))
        sections.extend(["", "## Cards"])
        sections.extend(
            [
                _markdown_link(card.title, chapter_path, path)
                for path, card in zip(card_paths, cards, strict=True)
            ]
            or ["- None."]
        )
        return "\n".join(sections) + "\n"

    def _build_manifest(
        self,
        *,
        previous: ObsidianBookManifest | None,
        book_key: str,
        book_title: str,
        total_chapters: int,
        moc_path: str,
        current: ObsidianChapterManifest,
        cards: tuple[ObsidianCardManifest, ...],
    ) -> ObsidianBookManifest:
        old_chapters = (
            () if previous is None or previous.book_key != book_key else previous.chapters
        )
        old_cards = () if previous is None or previous.book_key != book_key else previous.cards
        chapters = tuple(
            sorted(
                (*(chapter for chapter in old_chapters if chapter.index != current.index), current),
                key=lambda item: item.index,
            )
        )
        current_ids = {card.stable_id for card in cards}
        remaining_cards = tuple(
            card
            for card in old_cards
            if card.chapter_index != current.index and card.stable_id not in current_ids
        )
        all_cards = tuple(
            sorted(
                (*remaining_cards, *cards),
                key=lambda item: (item.chapter_index, item.kind, item.stable_id),
            )
        )
        provisional = ObsidianBookManifest(
            schema=_SCHEMA,
            book_key=book_key,
            book_title=book_title,
            book_directory=moc_path.rsplit("/", 1)[0],
            moc_path=moc_path,
            total_chapters=max(
                total_chapters,
                current.index + 1,
                *(chapter.index + 1 for chapter in old_chapters),
                previous.total_chapters
                if previous is not None and previous.book_key == book_key
                else 0,
            ),
            chapters=chapters,
            cards=all_cards,
        )
        return provisional.model_copy(update={"checksum": _manifest_checksum(provisional)})

    def _moc_body(self, manifest: ObsidianBookManifest, moc_path: str) -> str:
        sections = [f"# {_markdown_text(manifest.book_title)} MOC", "", "## Coverage"]
        known = str(manifest.total_chapters) if manifest.total_chapters else "unknown"
        sections.extend(_list((f"Rendered: {len(manifest.chapters)} / known total: {known}.",)))
        sections.extend(["", "## Unprocessed chapters"])
        processed_indices = {chapter.index for chapter in manifest.chapters}
        missing: list[str] = []
        missing_count = 0
        for index in range(manifest.total_chapters):
            if index in processed_indices:
                continue
            missing_count += 1
            if len(missing) < _MAX_MISSING_CHAPTER_PREVIEW:
                missing.append(f"{index + 1:02d}")
        remaining = missing_count - len(missing)
        if remaining:
            missing.append(f"{remaining:,} additional unprocessed chapters.")
        sections.extend(_list(missing))
        sections.extend(["", "## Chapter directory"])
        sections.extend(
            [
                _markdown_link(
                    f"{chapter.index + 1:02d} {chapter.title}", moc_path, chapter.note_path
                )
                for chapter in manifest.chapters
            ]
            or ["- None."]
        )
        frameworks = sorted(
            {framework for chapter in manifest.chapters for framework in chapter.frameworks}
        )
        sections.extend(["", "## Frameworks", *_list(frameworks), "", "## Topics"])
        topics = sorted({topic for chapter in manifest.chapters for topic in chapter.topics})
        sections.extend(_list(topics))
        sections.extend(["", "## Cards"])
        sections.extend(
            [_markdown_link(card.title, moc_path, card.path) for card in manifest.cards]
            or ["- None."]
        )
        return "\n".join(sections) + "\n"
