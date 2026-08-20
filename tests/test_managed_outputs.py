from __future__ import annotations

import hashlib
import json
import traceback
from collections.abc import Mapping

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
from cove_book_forge.outputs.obsidian_render import ObsidianRenderer, canonical_manifest_bytes


def _snapshot(
    *, index: int = 0, title: str = "Chapter one", book_title: str = "Managed book"
) -> ChapterSnapshot:
    return ChapterSnapshot(
        source_system="test-source",
        external_book_id="book-123",
        book=BookMetadata(title=book_title, author="A. Author", total_chapters=2),
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
    assert error.__cause__ is None


def _assert_raises_without_leak(operation, sentinel: str) -> None:
    try:
        operation()
    except ForgeException as error:
        _assert_safe_error(error, sentinel)
        # The caller's own test source naturally appears in a full traceback;
        # inspect the exception chain itself, which is what public adapters emit.
        assert sentinel not in "".join(traceback.format_exception_only(error))
    else:
        pytest.fail("expected safe ForgeException")


def _with_checksum(manifest):
    payload = manifest.model_dump(mode="json", by_alias=True, exclude={"checksum"})
    checksum = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return manifest.model_copy(update={"checksum": checksum})


def _replace_moc_fingerprint(data: bytes, fingerprint: str) -> bytes:
    """Keep the locked MOC body, replacing only its controlled manifest fingerprint."""
    _, raw_header, body = data.split(b"---\n", 2)
    fields: dict[str, str] = {}
    for line in raw_header.decode().splitlines():
        key, raw_value = line.split(": ", 1)
        fields[key] = json.loads(raw_value)
    fields["cove_source_fingerprint"] = fingerprint
    return (
        b"---\n"
        + b"".join(f"{key}: {json.dumps(value)}\n".encode() for key, value in fields.items())
        + b"---\n"
        + body
    )


def _rendered_with_manifest(rendered, manifest):
    files = dict(rendered.files)
    files[_manifest_path(rendered)] = canonical_manifest_bytes(manifest)
    files[rendered.moc_path] = _replace_moc_fingerprint(files[rendered.moc_path], manifest.checksum)
    return rendered.model_copy(update={"files": files, "manifest": manifest})


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


def test_parser_rejects_legacy_manifest_with_unbounded_chapter_count() -> None:
    rendered = _render()
    manifest_path = _manifest_path(rendered)
    payload = json.loads(rendered.files[manifest_path])
    payload["total_chapters"] = 5_001
    checksum_payload = {key: value for key, value in payload.items() if key != "checksum"}
    payload["checksum"] = hashlib.sha256(
        json.dumps(
            checksum_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    legacy = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    with pytest.raises(ForgeException) as raised:
        parse_obsidian_manifest(legacy)

    assert raised.value.code is ForgeErrorCode.CONFIG_INVALID


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


def test_update_rejects_a_renderer_that_drops_a_previously_rendered_chapter() -> None:
    """Dropping the previous manifest from render input must never schedule old chapter deletion."""
    first = _render(index=0, concepts=(Concept(term="Old", definition="First."),))
    dropped_history = _render(index=1, concepts=(Concept(term="New", definition="Second."),))

    _assert_raises_without_leak(
        lambda: plan_obsidian_update(first.manifest, dict(first.files), dropped_history), "Old"
    )


def test_update_rejects_a_manifest_that_rewrites_a_noncurrent_chapter_summary() -> None:
    """Changing an older chapter record could redirect ownership before its bytes are preserved."""
    first = _render(index=0, concepts=(Concept(term="Old", definition="First."),))
    current = ObsidianRenderer(ObsidianOutputConfig()).render(
        _snapshot(index=1),
        _analyzed(concepts=(Concept(term="New", definition="Second."),)),
        first.manifest,
    )
    rewritten_old = current.manifest.chapters[0].model_copy(
        update={"title": "PRIVATE-OLD-SENTINEL"}
    )
    changed_manifest = _with_checksum(
        current.manifest.model_copy(
            update={"chapters": (rewritten_old, current.manifest.chapters[1])}
        )
    )
    changed_files = dict(current.files)
    changed_files[_manifest_path(current)] = canonical_manifest_bytes(changed_manifest)
    untrusted = current.model_copy(update={"files": changed_files, "manifest": changed_manifest})

    _assert_raises_without_leak(
        lambda: plan_obsidian_update(first.manifest, dict(first.files), untrusted),
        "PRIVATE-OLD-SENTINEL",
    )


def test_update_rejects_an_added_cardless_noncurrent_chapter_record() -> None:
    """An added old chapter can become a deletion/removal target in a later update."""
    first = _render(index=0)
    current = ObsidianRenderer(ObsidianOutputConfig()).render(
        _snapshot(index=1), _analyzed(), first.manifest
    )
    added = current.manifest.chapters[0].model_copy(
        update={
            "index": 7,
            "note_path": "Books/Managed book--6d85fd8b00de6581/Chapters/08 extra.md",
        }
    )
    untrusted = _rendered_with_manifest(
        current,
        _with_checksum(
            current.manifest.model_copy(
                update={"chapters": (*current.manifest.chapters, added), "total_chapters": 8}
            )
        ),
    )

    _assert_raises_without_leak(
        lambda: plan_obsidian_update(first.manifest, dict(first.files), untrusted), "extra.md"
    )


def test_update_rejects_reordered_noncurrent_card_records() -> None:
    """Order is part of the canonical retained history and must not drift between updates."""
    first = _render(
        index=0,
        concepts=(
            Concept(term="Alpha", definition="One."),
            Concept(term="Beta", definition="Two."),
        ),
    )
    current = ObsidianRenderer(ObsidianOutputConfig()).render(
        _snapshot(index=1), _analyzed(), first.manifest
    )
    old_cards = tuple(card for card in current.manifest.cards if card.chapter_index == 0)
    reordered = _rendered_with_manifest(
        current,
        _with_checksum(
            current.manifest.model_copy(
                update={
                    "cards": (
                        *reversed(old_cards),
                        *(card for card in current.manifest.cards if card.chapter_index == 1),
                    )
                }
            )
        ),
    )

    _assert_raises_without_leak(
        lambda: plan_obsidian_update(first.manifest, dict(first.files), reordered), "Alpha"
    )


def test_first_publish_rejects_a_renderer_with_multiple_chapter_records() -> None:
    """Without a previous manifest, an aggregate render cannot establish safe ownership history."""
    first = _render(index=0)
    aggregate = ObsidianRenderer(ObsidianOutputConfig()).render(
        _snapshot(index=1), _analyzed(), first.manifest
    )

    _assert_raises_without_leak(lambda: plan_obsidian_update(None, {}, aggregate), "Managed book")


def test_update_locks_the_book_root_and_moc_from_the_previous_manifest() -> None:
    """A title-derived root replacement could otherwise remove a valid book bundle."""
    first = _render()
    replacement = ObsidianRenderer(ObsidianOutputConfig()).render(
        _snapshot(book_title="Different display title"), _analyzed(), None
    )

    _assert_raises_without_leak(
        lambda: plan_obsidian_update(first.manifest, dict(first.files), replacement),
        "Different display title",
    )


def test_manifest_rejects_a_card_path_that_collides_with_its_manifest_path() -> None:
    """Allowing the manifest path into the ownership set could overwrite control state as a card."""
    rendered = _render(concepts=(Concept(term="One", definition="One definition."),))
    manifest_path = _manifest_path(rendered)
    original_card = rendered.manifest.cards[0]
    collision_card = original_card.model_copy(update={"path": manifest_path})
    original_chapter = rendered.manifest.chapters[0]
    collision_chapter = original_chapter.model_copy(update={"card_paths": (manifest_path,)})
    collision_manifest = _with_checksum(
        rendered.manifest.model_copy(
            update={"cards": (collision_card,), "chapters": (collision_chapter,)}
        )
    )

    _assert_raises_without_leak(
        lambda: parse_obsidian_manifest(canonical_manifest_bytes(collision_manifest)), manifest_path
    )


def test_manifest_rejects_moc_path_that_is_the_manifest_path() -> None:
    """A set silently deduplicates MOC/control-path equality unless it is rejected explicitly."""
    rendered = _render()
    manifest_path = _manifest_path(rendered)
    moved_chapter = rendered.manifest.chapters[0].model_copy(
        update={"note_path": ".cove-book-forge/obsidian/Chapters/01 chapter.md"}
    )
    collision_manifest = _with_checksum(
        rendered.manifest.model_copy(
            update={
                "book_directory": ".cove-book-forge/obsidian",
                "moc_path": manifest_path,
                "chapters": (moved_chapter,),
            }
        )
    )

    _assert_raises_without_leak(
        lambda: parse_obsidian_manifest(canonical_manifest_bytes(collision_manifest)), manifest_path
    )


def test_manifest_rejects_duplicate_current_card_ownership_records() -> None:
    """Two current-card records with the same stable ID would make update ownership ambiguous."""
    rendered = _render(concepts=(Concept(term="One", definition="One definition."),))
    duplicated = _with_checksum(
        rendered.manifest.model_copy(
            update={"cards": (rendered.manifest.cards[0], rendered.manifest.cards[0])}
        )
    )

    _assert_raises_without_leak(
        lambda: parse_obsidian_manifest(canonical_manifest_bytes(duplicated)),
        rendered.manifest.cards[0].stable_id,
    )


def test_renderer_sanitizes_percent_from_all_path_display_components() -> None:
    """A percent in a generated name would violate the portable path contract after rendering."""
    rendered = ObsidianRenderer(ObsidianOutputConfig()).render(
        _snapshot(title="Chapter% title", book_title="Book% title"),
        _analyzed(concepts=(Concept(term="Concept% title", definition="Safe."),)),
        None,
    )

    assert all("%" not in path for path in rendered.files)
    chapter = rendered.files[rendered.chapter_path].decode()
    assert "Chapter% title" in chapter


def test_planner_rejects_unsafe_unrelated_mapping_keys_before_any_lookup() -> None:
    """Ignoring unrelated unsafe keys would make a future publisher consume an unvalidated snapshot."""
    rendered = _render()

    _assert_raises_without_leak(
        lambda: plan_obsidian_update(None, {"../private-sentinel": b"x"}, rendered),
        "private-sentinel",
    )


def test_parser_rejects_non_bytes_manifest_input_without_leaking_its_representation() -> None:
    """Calling decode on arbitrary objects would expose their private repr through an exception."""

    class PrivatePayload:
        def __repr__(self) -> str:
            return "PRIVATE-MANIFEST-SENTINEL"

    _assert_raises_without_leak(
        lambda: parse_obsidian_manifest(PrivatePayload()), "PRIVATE-MANIFEST-SENTINEL"
    )


def test_planner_converts_mapping_iteration_failures_to_safe_errors() -> None:
    """A hostile Mapping must not leak an exception while the planner snapshots candidate bytes."""

    class ExplodingMapping(Mapping[str, bytes]):
        def __getitem__(self, key: str) -> bytes:
            raise RuntimeError("PRIVATE-MAPPING-SENTINEL")

        def __iter__(self):
            raise RuntimeError("PRIVATE-MAPPING-SENTINEL")

        def __len__(self) -> int:
            return 1

    _assert_raises_without_leak(
        lambda: plan_obsidian_update(None, ExplodingMapping(), _render()),
        "PRIVATE-MAPPING-SENTINEL",
    )


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
