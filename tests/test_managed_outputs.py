from __future__ import annotations

import hashlib
import json

import pytest

from cove_book_forge.config import ObsidianOutputConfig
from cove_book_forge.contracts import (
    AnalyzedChapter,
    BookMetadata,
    ChapterAnalysis,
    ChapterContent,
    ChapterSnapshot,
)
from cove_book_forge.contracts.analysis import Concept
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs.managed import (
    parse_managed_markdown,
    parse_obsidian_manifest,
    plan_obsidian_update,
)
from cove_book_forge.outputs.obsidian_render import ObsidianRenderer


def _snapshot(*, index: int = 0, title: str = "Chapter one") -> ChapterSnapshot:
    return ChapterSnapshot(
        source_system="test-source",
        external_book_id="book-123",
        book=BookMetadata(title="Managed book", author="A. Author", total_chapters=2),
        chapter=ChapterContent(index=index, title=title, content="Source text."),
    )


def _analyzed(
    *, fingerprint: str = "a" * 64, concepts: tuple[Concept, ...] = ()
) -> AnalyzedChapter:
    return AnalyzedChapter(
        input_fingerprint=fingerprint,
        cache_hit=True,
        analysis=ChapterAnalysis(core_idea="A useful point.", concepts=concepts),
    )


def _render(*, index: int = 0, title: str = "Chapter one", concepts: tuple[Concept, ...] = ()):
    return ObsidianRenderer(ObsidianOutputConfig()).render(
        _snapshot(index=index, title=title), _analyzed(concepts=concepts), None
    )


def _manifest_path(rendered: object) -> str:
    files = rendered.files  # type: ignore[attr-defined]
    return next(path for path in files if path.endswith(".json"))


def _assert_safe_error(error: ForgeException, sentinel: str) -> None:
    assert error.code in {ForgeErrorCode.CONFIG_INVALID, ForgeErrorCode.EXTERNAL_MODIFICATION}
    public = repr(error) + str(error) + json.dumps(error.as_result(), ensure_ascii=False)
    assert sentinel not in public
    assert "/Users/private" not in public
    assert "Traceback" not in public


def test_parses_a_locked_managed_markdown_round_trip() -> None:
    """Removing the exact frontmatter contract must make this fail."""
    rendered = _render()

    parsed = parse_managed_markdown(rendered.files[rendered.chapter_path])

    assert parsed.kind == "chapter"
    assert parsed.book_key == rendered.manifest.book_key
    assert (
        parsed.body
        == b"# Chapter one\n\n## Source\n- Book: Managed book\n- Author: A. Author\n- Source: test-source\n\n## Core idea\nA useful point.\n\n## Frameworks\n- None.\n\n## Concepts\n- None.\n\n## Mental models\n- None.\n\n## Methods\n- None.\n\n## Anti-patterns\n- None.\n\n## Decision rules\n- None.\n\n## Worked examples\n- None.\n\n## Key takeaways\n- None.\n\n## Highlights\n- None.\n\n## User notes\n- None.\n\n## Annotations and reflections\n- None.\n\n## Evidence\n- None.\n\n## Quality warnings\n- None.\n\n## Cards\n- None.\n"
    )


@pytest.mark.parametrize(
    ("mutate", "sentinel"),
    [
        (lambda data: data.replace(b"cove_kind", b"cove_extra", 1), "cove_extra"),
        (lambda data: data.replace(b'cove_schema: "1"', b"cove_schema: 1", 1), "schema: 1"),
        (lambda data: data + b"changed body", "changed body"),
        (lambda data: b"----\n" + data[4:], "----"),
    ],
)
def test_rejects_malformed_or_externally_changed_markdown_without_leaking_it(
    mutate, sentinel: str
) -> None:
    """Accepting an altered marker, scalar, or hash would overwrite user content."""
    rendered = _render()

    with pytest.raises(ForgeException) as raised:
        parse_managed_markdown(mutate(rendered.files[rendered.chapter_path]))

    _assert_safe_error(raised.value, sentinel)


def test_parser_allows_a_body_delimiter_after_the_locked_frontmatter() -> None:
    """Treating later Markdown delimiters as frontmatter would reject valid source text."""
    rendered = _render()
    data = rendered.files[rendered.chapter_path]
    frontmatter, body = data.split(b"---\n", 2)[1:]
    changed_body = body + b"\n---\nThis is Markdown body content.\n"
    fields = json.loads(
        "{"
        + ",".join(
            json.dumps(line.split(": ", 1)[0]) + ":" + line.split(": ", 1)[1]
            for line in frontmatter.decode().splitlines()
        )
        + "}"
    )
    fields["cove_body_sha256"] = hashlib.sha256(changed_body).hexdigest()
    replacement = (
        b"---\n"
        + b"".join(f"{key}: {json.dumps(value)}\n".encode() for key, value in fields.items())
        + b"---\n"
        + changed_body
    )

    assert parse_managed_markdown(replacement).body == changed_body


def test_parses_only_canonical_manifest_with_matching_checksum_and_internal_paths() -> None:
    """Skipping canonical/checksum/path validation would permit an untrusted ownership map."""
    rendered = _render()
    manifest_path = _manifest_path(rendered)

    parsed = parse_obsidian_manifest(rendered.files[manifest_path])

    assert parsed == rendered.manifest
    payload = json.loads(rendered.files[manifest_path])
    payload["moc_path"] = "../private/secret.md"
    with pytest.raises(ForgeException) as raised:
        parse_obsidian_manifest(json.dumps(payload).encode())
    _assert_safe_error(raised.value, "secret.md")


def test_rejects_manifest_duplicate_and_cross_book_references() -> None:
    """Duplicate ownership or cross-book note paths could delete another bundle's content."""
    rendered = _render()
    manifest_path = _manifest_path(rendered)
    payload = json.loads(rendered.files[manifest_path])
    payload["chapters"].append(payload["chapters"][0])
    with pytest.raises(ForgeException) as raised:
        parse_obsidian_manifest(json.dumps(payload).encode())
    _assert_safe_error(raised.value, rendered.chapter_path)

    payload = json.loads(rendered.files[manifest_path])
    payload["chapters"][0]["note_path"] = "Books/other--0123456789abcdef/Chapters/01 other.md"
    with pytest.raises(ForgeException) as raised:
        parse_obsidian_manifest(json.dumps(payload).encode())
    _assert_safe_error(raised.value, "other--0123456789abcdef")


def test_first_publish_rejects_an_occupied_unmanaged_target() -> None:
    """A first publish must never adopt a user-created file at a target path."""
    rendered = _render()
    files = {rendered.chapter_path: b"private body"}

    with pytest.raises(ForgeException) as raised:
        plan_obsidian_update(None, files, rendered)

    _assert_safe_error(raised.value, "private body")


def test_first_publish_does_not_adopt_preexisting_managed_bytes_without_a_manifest() -> None:
    """Treating a marker alone as ownership would adopt another book's managed output."""
    rendered = _render()

    with pytest.raises(ForgeException) as raised:
        plan_obsidian_update(None, dict(rendered.files), rendered)

    _assert_safe_error(raised.value, rendered.manifest.book_key)


def test_same_bundle_is_a_zero_write_plan() -> None:
    """Writing an identical bundle would break the no-rewrite publication guarantee."""
    rendered = _render()

    plan = plan_obsidian_update(rendered.manifest, dict(rendered.files), rendered)

    assert plan.unchanged is True
    assert dict(plan.writes) == {}
    assert plan.removals == ()
    assert plan.changed_paths == ()


def test_update_rewrites_only_the_current_bundle_and_preserves_old_chapter_bytes() -> None:
    """Rewriting an unchanged older chapter would lose its timestamp and violate incremental output."""
    first = _render(index=0, concepts=(Concept(term="Old", definition="First."),))
    second = ObsidianRenderer(ObsidianOutputConfig()).render(
        _snapshot(index=1, title="Chapter two"),
        _analyzed(fingerprint="b" * 64, concepts=(Concept(term="New", definition="Second."),)),
        first.manifest,
    )
    existing = dict(first.files)

    plan = plan_obsidian_update(first.manifest, existing, second)

    assert first.chapter_path not in plan.writes
    assert next(path for path in first.card_paths) not in plan.writes
    assert second.chapter_path in plan.writes
    assert second.moc_path in plan.writes
    assert _manifest_path(second) in plan.writes
    assert plan.removals == ()


def test_update_stops_for_tampered_old_file_and_removes_only_verified_stale_cards() -> None:
    """Ignoring a modified stale card would delete user edits; untouched stale cards may be removed."""
    concept = Concept(term="Old", definition="First.")
    first = _render(concepts=(concept,))
    replacement = _render(concepts=())
    existing = dict(first.files)
    existing[first.card_paths[0]] = existing[first.card_paths[0]] + b"\nprivate edit"

    with pytest.raises(ForgeException) as raised:
        plan_obsidian_update(first.manifest, existing, replacement)
    _assert_safe_error(raised.value, "private edit")

    plan = plan_obsidian_update(first.manifest, dict(first.files), replacement)
    assert plan.removals == first.card_paths


def test_renamed_current_chapter_removes_only_the_verified_old_note() -> None:
    """Leaving the old title path would duplicate a chapter after a harmless rename."""
    first = _render(title="Original title")
    renamed = ObsidianRenderer(ObsidianOutputConfig()).render(
        _snapshot(title="Renamed title"), _analyzed(), first.manifest
    )

    plan = plan_obsidian_update(first.manifest, dict(first.files), renamed)

    assert first.chapter_path in plan.removals
    assert renamed.chapter_path in plan.writes
    assert first.moc_path not in plan.removals


@pytest.mark.parametrize(
    "unsafe", ["/tmp/x", "../x", "Cards\\x.md", "Cards/a%2fb.md", "Cards/CON.md", "Cards/a\x00.md"]
)
def test_planner_rejects_unsafe_paths_before_mapping_lookup(unsafe: str) -> None:
    """Looking up an unsafe key before validation could turn a mapping into filesystem traversal later."""
    rendered = _render()
    bad = rendered.model_copy(update={"files": {unsafe: b"x"}})

    with pytest.raises(ForgeException) as raised:
        plan_obsidian_update(None, {}, bad)

    _assert_safe_error(raised.value, unsafe)
