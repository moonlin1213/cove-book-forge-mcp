"""Strict parsing and pure update planning for managed Agent Skills."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

import pytest
from test_skill_render import _analyzed, _render, _snapshot

from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs.skill_managed import (
    parse_skill_manifest,
    plan_skill_update,
    validate_rendered_skill,
    validate_skill_bundle,
)
from cove_book_forge.outputs.skill_models import RenderedAgentSkill
from cove_book_forge.outputs.skill_render import canonical_manifest_bytes


def _error_code(error: pytest.ExceptionInfo[ForgeException]) -> ForgeErrorCode:
    assert str(error.value) == "Output changed outside this application."
    assert error.value.details == {}
    return error.value.code


def _files(rendered: RenderedAgentSkill) -> dict[str, bytes]:
    return dict(rendered.files)


def _manifest_bytes(payload: Mapping[str, object]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "checksum"}
    canonical_unsigned = json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    signed = {**unsigned, "checksum": hashlib.sha256(canonical_unsigned).hexdigest()}
    return json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _second(first: RenderedAgentSkill, *, title: str = "Second chapter") -> RenderedAgentSkill:
    from cove_book_forge.config import SkillOutputConfig
    from cove_book_forge.outputs import AgentSkillRenderer

    return AgentSkillRenderer(SkillOutputConfig()).render(
        _snapshot(chapter_index=1, title=title),
        _analyzed(fingerprint="b" * 64),
        first.manifest,
    )


def test_parse_manifest_requires_exact_canonical_json_and_checksum() -> None:
    rendered = _render()
    assert parse_skill_manifest(rendered.files[".cove-book-forge.json"]) == rendered.manifest

    pretty = json.dumps(
        rendered.manifest.model_dump(mode="json", by_alias=True),
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    duplicate = rendered.files[".cove-book-forge.json"].replace(
        b'{"author":', b'{"author":"competitor","author":', 1
    )
    bad_checksum = rendered.files[".cove-book-forge.json"].replace(
        rendered.manifest.checksum.encode(), b"0" * 64, 1
    )

    for invalid in (pretty, duplicate, bad_checksum, b"[]", b"\xff"):
        with pytest.raises(ForgeException) as error:
            parse_skill_manifest(invalid)
        assert _error_code(error) is ForgeErrorCode.EXTERNAL_MODIFICATION


def test_bundle_requires_complete_hash_agreement_and_exact_expected_tree() -> None:
    rendered = _render()
    files = _files(rendered)
    assert validate_skill_bundle(files) == rendered.manifest
    assert ".cove-book-forge.json" not in {item.path for item in rendered.manifest.files}
    assert "chapters/index.md" in {item.path for item in rendered.manifest.files}

    cases: list[dict[str, bytes]] = []
    missing = dict(files)
    missing.pop("chapters/index.md")
    cases.append(missing)
    extra = dict(files)
    extra["notes.md"] = b"unmanaged"
    cases.append(extra)
    altered = dict(files)
    altered[rendered.chapter_path] += b"tampered"
    cases.append(altered)
    manifest_hash = dict(files)
    payload = rendered.manifest.model_dump(mode="json", by_alias=True)
    payload["files"][0]["sha256"] = "0" * 64
    manifest_hash[".cove-book-forge.json"] = _manifest_bytes(payload)
    cases.append(manifest_hash)

    for invalid in cases:
        with pytest.raises(ForgeException) as error:
            validate_skill_bundle(invalid)
        assert _error_code(error) is ForgeErrorCode.EXTERNAL_MODIFICATION


def test_bundle_rejects_duplicate_manifest_paths_and_wrong_entry_kinds() -> None:
    rendered = _render()
    files = _files(rendered)
    raw = rendered.manifest.model_dump(mode="json", by_alias=True)
    raw["files"].append(dict(raw["files"][0]))
    files[".cove-book-forge.json"] = _manifest_bytes(raw)
    with pytest.raises(ForgeException) as error:
        validate_skill_bundle(files)
    assert _error_code(error) is ForgeErrorCode.EXTERNAL_MODIFICATION

    original = _files(rendered)
    for kind in ("symlink", "directory", "fifo"):
        kinds = {path: "file" for path in original}
        kinds[rendered.chapter_path] = kind
        with pytest.raises(ForgeException) as error:
            validate_skill_bundle(original, entry_kinds=kinds)
        assert _error_code(error) is ForgeErrorCode.EXTERNAL_MODIFICATION


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("../escape.md", b"bad"),
        ("chapters/../../escape.md", b"bad"),
        ("run.sh", b"#!/bin/sh\nexit 0\n"),
        ("hooks/config.json", b"{}"),
        ("agents/mcp.yaml", b"servers: {}\n"),
    ],
)
def test_rendered_validation_rejects_traversal_and_forbidden_file_shapes(
    path: str, payload: bytes
) -> None:
    rendered = _render()
    unsafe = RenderedAgentSkill.model_construct(
        files={**rendered.files, path: payload},
        manifest=rendered.manifest,
        skill_slug=rendered.skill_slug,
        chapter_path=rendered.chapter_path,
    )
    with pytest.raises(ForgeException) as error:
        validate_rendered_skill(unsafe)
    assert _error_code(error) is ForgeErrorCode.EXTERNAL_MODIFICATION


def test_bundle_rejects_bad_skill_and_openai_yaml_structures() -> None:
    rendered = _render()
    corruptions = (
        (
            "SKILL.md",
            rendered.files["SKILL.md"].replace(
                b"description: ", b'allowed-tools: "shell"\ndescription: ', 1
            ),
        ),
        (
            "SKILL.md",
            rendered.files["SKILL.md"].replace(rendered.skill_slug.encode(), b"wrong-slug", 1),
        ),
        (
            "agents/openai.yaml",
            rendered.files["agents/openai.yaml"] + b"\nallowed-tools: shell\n",
        ),
        (
            "agents/openai.yaml",
            rendered.files["agents/openai.yaml"].replace(b"interface:", b"mcpServers:", 1),
        ),
    )
    for path, payload in corruptions:
        files = _files(rendered)
        files[path] = payload
        with pytest.raises(ForgeException) as error:
            validate_skill_bundle(files)
        assert _error_code(error) is ForgeErrorCode.EXTERNAL_MODIFICATION


@pytest.mark.parametrize(
    "forbidden",
    [
        b"\x00",
        b"<script>alert(1)</script>",
        b"\nallowed-tools: shell\n",
        b"\n#!/usr/bin/env bash\n",
        b"](../../outside.md)",
        b"](/absolute.md)",
    ],
)
def test_bundle_scans_for_forbidden_content_and_escaping_links(forbidden: bytes) -> None:
    rendered = _render()
    files = _files(rendered)
    files[rendered.chapter_path] += forbidden
    with pytest.raises(ForgeException) as error:
        validate_skill_bundle(files)
    assert _error_code(error) is ForgeErrorCode.EXTERNAL_MODIFICATION


def test_first_update_plan_is_complete_and_unchanged_plan_has_zero_writes() -> None:
    rendered = _render()
    first = plan_skill_update(None, {}, rendered)
    assert first.complete_files == rendered.files
    assert set(first.changed_paths) == set(rendered.files)
    assert not first.unchanged

    unchanged = plan_skill_update(rendered.manifest, _files(rendered), rendered)
    assert unchanged.complete_files == rendered.files
    assert unchanged.changed_paths == ()
    assert unchanged.unchanged


def test_incremental_plan_preserves_verified_historical_bytes_and_drops_stale_current() -> None:
    first = _render()
    second = _second(first)

    plan = plan_skill_update(first.manifest, _files(first), second)

    assert plan.complete_files[first.chapter_path] == first.files[first.chapter_path]
    assert plan.complete_files[second.chapter_path] == second.files[second.chapter_path]
    assert validate_skill_bundle(plan.complete_files) == second.manifest

    renamed = _second(first, title="Renamed first").model_copy(
        update={"chapter_path": second.chapter_path}
    )
    assert renamed.skill_slug == first.skill_slug


def test_current_chapter_rename_removes_the_stale_managed_path() -> None:
    first = _render()
    from cove_book_forge.config import SkillOutputConfig
    from cove_book_forge.outputs import AgentSkillRenderer

    renamed = AgentSkillRenderer(SkillOutputConfig()).render(
        _snapshot(title="New chapter title"),
        _analyzed(fingerprint="c" * 64),
        first.manifest,
    )
    plan = plan_skill_update(first.manifest, _files(first), renamed)
    assert first.chapter_path not in plan.complete_files
    assert renamed.chapter_path in plan.complete_files
    assert first.chapter_path in plan.changed_paths


def test_update_fails_closed_on_slug_identity_history_or_existing_bundle_changes() -> None:
    first = _render()
    second = _second(first)
    invalid_renders = (
        second.model_copy(update={"skill_slug": "other-book--0000000000000000"}),
        second.model_copy(
            update={
                "manifest": second.manifest.model_copy(
                    update={"skill_slug": "other-book--0000000000000000"}
                )
            }
        ),
        second.model_copy(
            update={"manifest": second.manifest.model_copy(update={"book_key": "0" * 16})}
        ),
    )
    existing_cases = []
    missing = _files(first)
    missing.pop(first.chapter_path)
    existing_cases.append(missing)
    tampered = _files(first)
    tampered[first.chapter_path] += b"tampered"
    existing_cases.append(tampered)

    for invalid in invalid_renders:
        with pytest.raises(ForgeException) as error:
            plan_skill_update(first.manifest, _files(first), invalid)
        assert _error_code(error) is ForgeErrorCode.EXTERNAL_MODIFICATION
    for existing in existing_cases:
        with pytest.raises(ForgeException) as error:
            plan_skill_update(first.manifest, existing, second)
        assert _error_code(error) is ForgeErrorCode.EXTERNAL_MODIFICATION


def test_validation_budgets_file_count_individual_bytes_and_total_bytes() -> None:
    rendered = _render()
    too_many = _files(rendered)
    for index in range(5_020):
        too_many[f"extra-{index}.md"] = b"x"
    huge = _files(rendered)
    huge[rendered.chapter_path] = b"x" * (8 * 1024 * 1024 + 1)

    for invalid in (too_many, huge):
        with pytest.raises(ForgeException) as error:
            validate_skill_bundle(invalid)
        assert _error_code(error) is ForgeErrorCode.EXTERNAL_MODIFICATION


def test_manifest_bytes_are_still_renderer_canonical_after_round_trip() -> None:
    rendered = _render()
    parsed = parse_skill_manifest(rendered.files[".cove-book-forge.json"])
    assert canonical_manifest_bytes(parsed) == rendered.files[".cove-book-forge.json"]
