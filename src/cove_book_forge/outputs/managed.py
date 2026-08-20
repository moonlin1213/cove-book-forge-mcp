"""Pure validation and conflict planning for managed Obsidian output.

This module deliberately accepts bytes mappings instead of paths.  The publisher is
responsible for filesystem checks and atomic replacement; these functions establish
the complete, safe set of replacements before it makes any mutation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs.obsidian_models import (
    ObsidianBookManifest,
    ObsidianCardManifest,
    ObsidianChapterManifest,
    RenderedObsidianBook,
)
from cove_book_forge.outputs.obsidian_render import canonical_manifest_bytes
from cove_book_forge.path_safety import validate_relative_path

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
_HEX_16 = re.compile(r"^[0-9a-f]{16}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class ManagedMarkdown:
    """The validated, non-user-controlled envelope around a Markdown body."""

    kind: str
    book_key: str
    chapter_index: int
    source_fingerprint: str
    stable_id: str
    body: bytes


@dataclass(frozen=True)
class OutputUpdatePlan:
    """A complete mutation plan, expressed without performing any I/O."""

    writes: Mapping[str, bytes]
    removals: tuple[str, ...]
    unchanged: bool
    changed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "writes", MappingProxyType(dict(self.writes)))


def _invalid() -> ForgeException:
    return ForgeException(ForgeErrorCode.CONFIG_INVALID, "managed output is invalid")


def _modified() -> ForgeException:
    return ForgeException(ForgeErrorCode.EXTERNAL_MODIFICATION, "managed output changed")


def _require_path(path: str, *, modified: bool = False) -> str:
    try:
        return validate_relative_path(path)
    except (TypeError, ValueError):
        raise _modified() if modified else _invalid() from None


def _load_json_object(data: bytes) -> dict[str, Any]:
    if not isinstance(data, bytes):
        raise _invalid()
    try:
        text = data.decode("utf-8")

        def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        value = json.loads(text, object_pairs_hook=no_duplicates)
    except Exception:
        raise _invalid() from None
    if not isinstance(value, dict):
        raise _invalid()
    return value


def parse_managed_markdown(data: bytes) -> ManagedMarkdown:
    """Parse the exact locked Markdown envelope, never interpreting its body."""
    if not isinstance(data, bytes) or not data.startswith(b"---\n"):
        raise _invalid()
    closing = data.find(b"---\n", 4)
    if closing < 0:
        raise _invalid()
    raw_header = data[4:closing]
    body = data[closing + 4 :]
    try:
        raw_header.decode("utf-8")
        body.decode("utf-8")
    except UnicodeDecodeError:
        raise _invalid() from None
    lines = raw_header.splitlines()
    if len(lines) != len(_MARKDOWN_FIELDS):
        raise _invalid()
    values: dict[str, str] = {}
    for expected, line in zip(_MARKDOWN_FIELDS, lines, strict=True):
        try:
            decoded = line.decode("utf-8")
            key, separator, raw_value = decoded.partition(": ")
            value = json.loads(raw_value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise _invalid() from None
        if (
            key != expected
            or not separator
            or not isinstance(value, str)
            or raw_value != json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            or key in values
        ):
            raise _invalid()
        values[key] = value
    if values["cove_book_forge"] != "managed" or values["cove_schema"] != "1":
        raise _invalid()
    kind = values["cove_kind"]
    book_key = values["cove_book_key"]
    fingerprint = values["cove_source_fingerprint"]
    stable_id = values["cove_stable_id"]
    try:
        chapter_index = int(values["cove_chapter_index"])
    except ValueError:
        raise _invalid() from None
    if (
        values["cove_chapter_index"] != str(chapter_index)
        or not _HEX_16.fullmatch(book_key)
        or not _HEX_64.fullmatch(fingerprint)
        or values["cove_body_sha256"] != hashlib.sha256(body).hexdigest()
    ):
        raise _invalid()
    if kind == "chapter":
        if chapter_index < 0 or stable_id != f"{book_key}-{chapter_index:04d}":
            raise _invalid()
    elif kind == "card":
        if chapter_index < 0 or not _HEX_16.fullmatch(stable_id):
            raise _invalid()
    elif kind == "moc":
        if chapter_index != -1 or stable_id != f"{book_key}-moc":
            raise _invalid()
    else:
        raise _invalid()
    return ManagedMarkdown(kind, book_key, chapter_index, fingerprint, stable_id, body)


def _validate_manifest_references(manifest: ObsidianBookManifest) -> None:
    book_directory = _require_path(manifest.book_directory)
    moc_path = _require_path(manifest.moc_path)
    if (
        not moc_path.startswith(f"{book_directory}/")
        or moc_path.rsplit("/", 1)[0] != book_directory
    ):
        raise _invalid()
    chapters: dict[int, ObsidianChapterManifest] = {}
    cards_by_path: dict[str, ObsidianCardManifest] = {}
    card_ids: set[str] = set()
    managed_paths = {moc_path, _manifest_path(manifest.book_key)}
    for chapter in manifest.chapters:
        note_path = _require_path(chapter.note_path)
        if (
            chapter.index in chapters
            or note_path in managed_paths
            or not note_path.startswith(f"{book_directory}/Chapters/")
        ):
            raise _invalid()
        chapters[chapter.index] = chapter
        managed_paths.add(note_path)
    if manifest.total_chapters < max(
        (chapter.index + 1 for chapter in chapters.values()), default=0
    ):
        raise _invalid()
    for card in manifest.cards:
        card_path = _require_path(card.path)
        if card_path in managed_paths or card_path in cards_by_path or card.stable_id in card_ids:
            raise _invalid()
        if card.chapter_index not in chapters:
            raise _invalid()
        cards_by_path[card_path] = card
        card_ids.add(card.stable_id)
    seen_card_paths: set[str] = set()
    for chapter in chapters.values():
        if len(set(chapter.card_paths)) != len(chapter.card_paths):
            raise _invalid()
        for path in chapter.card_paths:
            card_path = _require_path(path)
            referenced_card = cards_by_path.get(card_path)
            if (
                referenced_card is None
                or referenced_card.chapter_index != chapter.index
                or card_path in seen_card_paths
            ):
                raise _invalid()
            seen_card_paths.add(card_path)
    if seen_card_paths != set(cards_by_path):
        raise _invalid()


def parse_obsidian_manifest(data: bytes) -> ObsidianBookManifest:
    """Parse only exact canonical v1 manifests with a verified checksum."""
    if not isinstance(data, bytes):
        raise _invalid()
    _load_json_object(data)
    try:
        # JSON arrays are the canonical representation of the immutable tuple fields.
        # ``model_validate`` in strict Python mode intentionally rejects those arrays.
        manifest = ObsidianBookManifest.model_validate_json(data)
    except Exception:
        raise _invalid() from None
    expected = canonical_manifest_bytes(manifest)
    if data != expected:
        raise _invalid()
    payload_without_checksum = manifest.model_dump(mode="json", by_alias=True, exclude={"checksum"})
    canonical = json.dumps(
        payload_without_checksum,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if manifest.checksum != hashlib.sha256(canonical).hexdigest():
        raise _invalid()
    _validate_manifest_references(manifest)
    return manifest


def _manifest_path(book_key: str) -> str:
    return f".cove-book-forge/obsidian/{book_key}.json"


def _manifest_paths(manifest: ObsidianBookManifest) -> tuple[str, ...]:
    return (
        manifest.moc_path,
        *(chapter.note_path for chapter in manifest.chapters),
        *(card.path for card in manifest.cards),
        _manifest_path(manifest.book_key),
    )


def _expected_markdown(
    markdown: ManagedMarkdown,
    *,
    kind: str,
    book_key: str,
    chapter_index: int,
    stable_id: str,
    fingerprint: str,
) -> None:
    if (
        markdown.kind != kind
        or markdown.book_key != book_key
        or markdown.chapter_index != chapter_index
        or markdown.stable_id != stable_id
        or markdown.source_fingerprint != fingerprint
    ):
        raise _modified()


def _validate_existing_bundle(
    manifest: ObsidianBookManifest, existing: Mapping[str, bytes]
) -> None:
    manifest_path = _manifest_path(manifest.book_key)
    _require_path(manifest_path, modified=True)
    manifest_data = existing.get(manifest_path)
    if manifest_data is None or not isinstance(manifest_data, bytes):
        raise _modified()
    try:
        parsed = parse_obsidian_manifest(manifest_data)
    except ForgeException:
        raise _modified() from None
    if parsed != manifest:
        raise _modified()
    checks: list[tuple[str, str, int, str, str]] = [
        (manifest.moc_path, "moc", -1, f"{manifest.book_key}-moc", manifest.checksum),
    ]
    checks.extend(
        (
            chapter.note_path,
            "chapter",
            chapter.index,
            f"{manifest.book_key}-{chapter.index:04d}",
            chapter.input_fingerprint,
        )
        for chapter in manifest.chapters
    )
    chapter_fingerprints = {
        chapter.index: chapter.input_fingerprint for chapter in manifest.chapters
    }
    checks.extend(
        (
            card.path,
            "card",
            card.chapter_index,
            card.stable_id,
            chapter_fingerprints[card.chapter_index],
        )
        for card in manifest.cards
    )
    for path, kind, chapter_index, stable_id, fingerprint in checks:
        _require_path(path, modified=True)
        data = existing.get(path)
        if data is None or not isinstance(data, bytes):
            raise _modified()
        try:
            markdown = parse_managed_markdown(data)
            _expected_markdown(
                markdown,
                kind=kind,
                book_key=manifest.book_key,
                chapter_index=chapter_index,
                stable_id=stable_id,
                fingerprint=fingerprint,
            )
        except ForgeException:
            raise _modified() from None


def _current_chapter(manifest: ObsidianBookManifest, chapter_path: str) -> ObsidianChapterManifest:
    matches = [chapter for chapter in manifest.chapters if chapter.note_path == chapter_path]
    if len(matches) != 1:
        raise _invalid()
    return matches[0]


def _validate_new_bundle(
    rendered: RenderedObsidianBook,
) -> tuple[tuple[str, ...], ObsidianChapterManifest]:
    manifest = rendered.manifest
    _validate_manifest_references(manifest)
    manifest_path = _manifest_path(manifest.book_key)
    required = {
        manifest_path,
        rendered.moc_path,
        rendered.chapter_path,
        *rendered.card_paths,
    }
    for path in rendered.files:
        _require_path(path)
    if set(rendered.files) != required:
        raise _invalid()
    try:
        parsed_manifest = parse_obsidian_manifest(rendered.files[manifest_path])
    except (KeyError, ForgeException):
        raise _invalid() from None
    if parsed_manifest != manifest or rendered.moc_path != manifest.moc_path:
        raise _invalid()
    current_chapter = _current_chapter(manifest, rendered.chapter_path)
    if tuple(current_chapter.card_paths) != rendered.card_paths:
        raise _invalid()
    expected: list[tuple[str, str, int, str, str]] = [
        (rendered.moc_path, "moc", -1, f"{manifest.book_key}-moc", manifest.checksum),
        (
            rendered.chapter_path,
            "chapter",
            current_chapter.index,
            f"{manifest.book_key}-{current_chapter.index:04d}",
            current_chapter.input_fingerprint,
        ),
    ]
    cards = {card.path: card for card in manifest.cards}
    for path in rendered.card_paths:
        card = cards.get(path)
        if card is None:
            raise _invalid()
        expected.append(
            (path, "card", card.chapter_index, card.stable_id, current_chapter.input_fingerprint)
        )
    for path, kind, chapter_index, stable_id, fingerprint in expected:
        try:
            markdown = parse_managed_markdown(rendered.files[path])
            _expected_markdown(
                markdown,
                kind=kind,
                book_key=manifest.book_key,
                chapter_index=chapter_index,
                stable_id=stable_id,
                fingerprint=fingerprint,
            )
        except (KeyError, ForgeException):
            raise _invalid() from None
    # A renderer only carries bytes for the current chapter, its cards, MOC, and
    # manifest.  The manifest deliberately retains summaries for prior chapters;
    # those owned paths remain desired even though they must not be rewritten.
    return tuple(sorted(_manifest_paths(manifest))), current_chapter


def _validate_preserved_history(
    previous: ObsidianBookManifest,
    current: ObsidianBookManifest,
    current_chapter: ObsidianChapterManifest,
) -> None:
    """Prove that a current-chapter render did not discard another chapter's state."""
    if (
        previous.book_key != current.book_key
        or previous.book_directory != current.book_directory
        or previous.moc_path != current.moc_path
    ):
        raise _invalid()
    current_index = current_chapter.index
    old_chapters = {chapter.index: chapter for chapter in previous.chapters}
    new_chapters = {chapter.index: chapter for chapter in current.chapters}
    for index, chapter in old_chapters.items():
        if index != current_index and new_chapters.get(index) != chapter:
            raise _invalid()
    old_cards = {card for card in previous.cards if card.chapter_index != current_index}
    new_cards = {card for card in current.cards if card.chapter_index != current_index}
    if old_cards != new_cards:
        raise _invalid()


def _snapshot_existing(existing: Mapping[str, bytes]) -> dict[str, bytes]:
    """Copy untrusted mapping input once, validating every key and value first."""
    if not isinstance(existing, Mapping):
        raise _invalid()
    try:
        snapshot: dict[str, bytes] = {}
        for key, value in existing.items():
            if not isinstance(key, str) or not isinstance(value, bytes):
                raise _invalid()
            _require_path(key)
            snapshot[key] = value
        return snapshot
    except ForgeException:
        raise _invalid() from None
    except Exception:
        raise _invalid() from None


def plan_obsidian_update(
    previous: ObsidianBookManifest | None,
    existing: Mapping[str, bytes],
    rendered: RenderedObsidianBook,
) -> OutputUpdatePlan:
    """Return an all-or-nothing update plan without touching the filesystem.

    ``existing`` is purposely a mapping of candidate bytes.  Callers must not pass a
    directory abstraction here: every key is validated before the function reads it.
    """
    existing_snapshot = _snapshot_existing(existing)
    desired_owned_paths, current_chapter = _validate_new_bundle(rendered)
    write_candidates = tuple(sorted(rendered.files))
    if previous is None:
        if (
            len(rendered.manifest.chapters) != 1
            or rendered.manifest.chapters[0] != current_chapter
            or any(card.chapter_index != current_chapter.index for card in rendered.manifest.cards)
        ):
            raise _invalid()
        for path in desired_owned_paths:
            if path in existing_snapshot:
                raise _modified()
        writes = {path: rendered.files[path] for path in write_candidates}
        return OutputUpdatePlan(writes, (), False, tuple(sorted(writes)))
    _validate_manifest_references(previous)
    _validate_preserved_history(previous, rendered.manifest, current_chapter)
    _validate_existing_bundle(previous, existing_snapshot)
    owned_paths = set(_manifest_paths(previous))
    for path in write_candidates:
        _require_path(path)
        if path in existing_snapshot and path not in owned_paths:
            raise _modified()
    writes = {
        path: rendered.files[path]
        for path in write_candidates
        if existing_snapshot.get(path) != rendered.files[path]
    }
    removals = tuple(sorted(owned_paths - set(desired_owned_paths)))
    changed_paths = tuple(sorted((*writes, *removals)))
    return OutputUpdatePlan(writes, removals, not changed_paths, changed_paths)
