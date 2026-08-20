"""Strict parsing and pure complete-generation planning for managed Agent Skills."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Mapping
from typing import Any, Final

from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs.skill_models import (
    MAX_SKILL_FILES,
    AgentSkillChapterManifest,
    AgentSkillManifest,
    RenderedAgentSkill,
    SkillUpdatePlan,
)
from cove_book_forge.outputs.skill_render import canonical_manifest_bytes
from cove_book_forge.path_safety import validate_relative_path

_MANIFEST_PATH: Final = ".cove-book-forge.json"
_ROOT_FILES: Final = frozenset(
    {
        "SKILL.md",
        "agents/openai.yaml",
        "chapters/index.md",
        "glossary.md",
        "patterns.md",
        "cheatsheet.md",
    }
)
_MAX_MANIFEST_BYTES: Final = 2 * 1024 * 1024
_MAX_FILE_BYTES: Final = 8 * 1024 * 1024
_MAX_TOTAL_BYTES: Final = 64 * 1024 * 1024
_LINK = re.compile(r"\]\(([^)]+)\)")
_FORBIDDEN_BYTES: Final = (
    b"\x00",
    b"<script",
    b"allowed-tools:",
    b"mcpservers:",
    b"#!/usr/bin/",
    b"#!/bin/",
)


def _modified() -> ForgeException:
    return ForgeException(ForgeErrorCode.EXTERNAL_MODIFICATION, "managed Skill changed")


def _load_unique_object(data: bytes) -> dict[str, Any]:
    if not isinstance(data, bytes) or len(data) > _MAX_MANIFEST_BYTES:
        raise _modified()

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except Exception:
        raise _modified() from None
    if not isinstance(value, dict):
        raise _modified()
    return value


def parse_skill_manifest(data: bytes) -> AgentSkillManifest:
    """Parse only canonical schema-v1 manifest bytes with a valid self-checksum."""
    _load_unique_object(data)
    try:
        manifest = AgentSkillManifest.model_validate_json(data)
    except Exception:
        raise _modified() from None
    if data != canonical_manifest_bytes(manifest):
        raise _modified()
    unsigned = manifest.model_dump(mode="json", by_alias=True, exclude={"checksum"})
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if manifest.checksum != hashlib.sha256(canonical).hexdigest():
        raise _modified()
    _expected_content_paths(manifest)
    return manifest


def _expected_content_paths(manifest: AgentSkillManifest) -> frozenset[str]:
    try:
        validated = AgentSkillManifest.model_validate(manifest.model_dump(by_alias=True))
    except Exception:
        raise _modified() from None
    if validated != manifest:
        raise _modified()
    chapter_paths = {chapter.chapter_path for chapter in manifest.chapters}
    expected = frozenset((*_ROOT_FILES, *chapter_paths))
    actual = frozenset(item.path for item in manifest.files)
    if (
        actual != expected
        or _MANIFEST_PATH in actual
        or len(manifest.files) != len(actual)
        or manifest.total_chapters
        < max((chapter.index + 1 for chapter in manifest.chapters), default=0)
    ):
        raise _modified()
    return expected


def _snapshot_files(files: Mapping[str, bytes]) -> dict[str, bytes]:
    if not isinstance(files, Mapping) or len(files) > MAX_SKILL_FILES + 1:
        raise _modified()
    snapshot: dict[str, bytes] = {}
    total = 0
    for path, payload in files.items():
        try:
            safe_path = validate_relative_path(path)
        except (TypeError, ValueError):
            raise _modified() from None
        if safe_path != path or not isinstance(payload, bytes) or len(payload) > _MAX_FILE_BYTES:
            raise _modified()
        total += len(payload)
        if total > _MAX_TOTAL_BYTES:
            raise _modified()
        snapshot[path] = payload
    return snapshot


def _require_file_kinds(paths: frozenset[str], entry_kinds: Mapping[str, str] | None) -> None:
    if entry_kinds is None:
        return
    if set(entry_kinds) != set(paths) or any(entry_kinds[path] != "file" for path in paths):
        raise _modified()


def _validate_skill_header(data: bytes, manifest: AgentSkillManifest) -> None:
    expected = (
        "---\n"
        f"name: {json.dumps(manifest.skill_slug, ensure_ascii=False, separators=(',', ':'))}\n"
        'description: "Apply analysed book references to a relevant task."\n'
        "---\n"
    ).encode()
    if not data.startswith(expected):
        raise _modified()


def _validate_openai_yaml(data: bytes, manifest: AgentSkillManifest) -> None:
    def quote(value: str) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    expected = "\n".join(
        (
            "interface:",
            f"  display_name: {quote(manifest.book_title)}",
            f"  short_description: {quote('Apply book knowledge to your task')}",
            f"  default_prompt: {quote(f'Use ${manifest.skill_slug} to apply {manifest.book_title} to this task.')}",
        )
    ).encode("utf-8")
    if data != expected:
        raise _modified()


def _validate_links(path: str, text: str, expected_paths: frozenset[str]) -> None:
    parent = posixpath.dirname(path)
    for target in _LINK.findall(text):
        if not target or target.startswith(("/", "\\")) or "\\" in target:
            raise _modified()
        try:
            validate_relative_path(target)
        except (TypeError, ValueError):
            raise _modified() from None
        resolved = posixpath.normpath(posixpath.join(parent, target))
        if resolved not in expected_paths:
            raise _modified()


def _scan_content(
    files: Mapping[str, bytes], manifest: AgentSkillManifest, expected_paths: frozenset[str]
) -> None:
    _validate_skill_header(files["SKILL.md"], manifest)
    _validate_openai_yaml(files["agents/openai.yaml"], manifest)
    for path, payload in files.items():
        if path == _MANIFEST_PATH:
            continue
        if not (path.endswith(".md") or path == "agents/openai.yaml"):
            raise _modified()
        lowered = payload.lower()
        if any(token in lowered for token in _FORBIDDEN_BYTES):
            raise _modified()
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise _modified() from None
        if path.endswith(".md"):
            _validate_links(path, text, expected_paths)


def _hashes(manifest: AgentSkillManifest) -> dict[str, str]:
    result = {item.path: item.sha256 for item in manifest.files}
    if len(result) != len(manifest.files):
        raise _modified()
    return result


def validate_skill_bundle(
    files: Mapping[str, bytes], *, entry_kinds: Mapping[str, str] | None = None
) -> AgentSkillManifest:
    """Validate one complete regular-file generation and return its manifest."""
    snapshot = _snapshot_files(files)
    if _MANIFEST_PATH not in snapshot:
        raise _modified()
    manifest = parse_skill_manifest(snapshot[_MANIFEST_PATH])
    expected_content = _expected_content_paths(manifest)
    expected_tree = frozenset((*expected_content, _MANIFEST_PATH))
    if frozenset(snapshot) != expected_tree:
        raise _modified()
    _require_file_kinds(expected_tree, entry_kinds)
    hashes = _hashes(manifest)
    if any(hashlib.sha256(snapshot[path]).hexdigest() != hashes[path] for path in expected_content):
        raise _modified()
    _scan_content(snapshot, manifest, expected_content)
    return manifest


def validate_rendered_skill(rendered: RenderedAgentSkill) -> AgentSkillManifest:
    """Validate Task-1's intentional current-chapter-plus-root incremental bundle."""
    try:
        strict_manifest = AgentSkillManifest.model_validate(
            rendered.manifest.model_dump(by_alias=True)
        )
    except Exception:
        raise _modified() from None
    if strict_manifest != rendered.manifest:
        raise _modified()
    snapshot = _snapshot_files(rendered.files)
    if rendered.skill_slug != rendered.manifest.skill_slug or _MANIFEST_PATH not in snapshot:
        raise _modified()
    parsed = parse_skill_manifest(snapshot[_MANIFEST_PATH])
    if parsed != rendered.manifest:
        raise _modified()
    chapters = [
        chapter
        for chapter in rendered.manifest.chapters
        if chapter.chapter_path == rendered.chapter_path
    ]
    if len(chapters) != 1:
        raise _modified()
    expected_rendered = frozenset((*_ROOT_FILES, rendered.chapter_path, _MANIFEST_PATH))
    if frozenset(snapshot) != expected_rendered:
        raise _modified()
    expected_content = _expected_content_paths(rendered.manifest)
    hashes = _hashes(rendered.manifest)
    for path in expected_rendered - {_MANIFEST_PATH}:
        if path not in hashes or hashlib.sha256(snapshot[path]).hexdigest() != hashes[path]:
            raise _modified()
    _scan_content(snapshot, rendered.manifest, expected_content)
    return rendered.manifest


def _current_chapter(rendered: RenderedAgentSkill) -> AgentSkillChapterManifest:
    matches = [
        chapter
        for chapter in rendered.manifest.chapters
        if chapter.chapter_path == rendered.chapter_path
    ]
    if len(matches) != 1:
        raise _modified()
    return matches[0]


def _require_preserved_history(previous: AgentSkillManifest, rendered: RenderedAgentSkill) -> None:
    current = _current_chapter(rendered)
    if (
        rendered.manifest.book_key != previous.book_key
        or rendered.manifest.skill_slug != previous.skill_slug
        or rendered.skill_slug != previous.skill_slug
        or rendered.manifest.total_chapters < previous.total_chapters
    ):
        raise _modified()
    old_chapters = {chapter.index: chapter for chapter in previous.chapters}
    new_chapters = {chapter.index: chapter for chapter in rendered.manifest.chapters}
    if set(new_chapters) != {*old_chapters, current.index}:
        raise _modified()
    old_hashes = _hashes(previous)
    new_hashes = _hashes(rendered.manifest)
    for index, chapter in old_chapters.items():
        if index == current.index:
            continue
        if new_chapters.get(index) != chapter or new_hashes.get(
            chapter.chapter_path
        ) != old_hashes.get(chapter.chapter_path):
            raise _modified()


def plan_skill_update(
    previous: AgentSkillManifest | None,
    existing: Mapping[str, bytes],
    rendered: RenderedAgentSkill,
) -> SkillUpdatePlan:
    """Build and validate a complete immutable generation without filesystem access."""
    validate_rendered_skill(rendered)
    existing_snapshot = _snapshot_files(existing)
    if previous is None:
        if existing_snapshot or len(rendered.manifest.chapters) != 1:
            raise _modified()
        initial_complete = dict(rendered.files)
        validate_skill_bundle(initial_complete)
        return SkillUpdatePlan(initial_complete, tuple(sorted(initial_complete)), False)

    parsed_previous = validate_skill_bundle(existing_snapshot)
    try:
        strict_previous = AgentSkillManifest.model_validate(previous.model_dump(by_alias=True))
    except Exception:
        raise _modified() from None
    if parsed_previous != strict_previous:
        raise _modified()
    _require_preserved_history(strict_previous, rendered)

    desired_paths = _expected_content_paths(rendered.manifest)
    rendered_snapshot = _snapshot_files(rendered.files)
    complete: dict[str, bytes] = {}
    for path in desired_paths:
        if path in rendered_snapshot:
            complete[path] = rendered_snapshot[path]
        elif path in existing_snapshot:
            complete[path] = existing_snapshot[path]
        else:
            raise _modified()
    complete[_MANIFEST_PATH] = rendered_snapshot[_MANIFEST_PATH]
    validate_skill_bundle(complete)
    changed = tuple(
        sorted(
            path
            for path in set(existing_snapshot) | set(complete)
            if existing_snapshot.get(path) != complete.get(path)
        )
    )
    return SkillUpdatePlan(complete, changed, not changed)
