from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from cove_book_forge.config import ObsidianOutputConfig
from cove_book_forge.contracts import (
    AnalyzedChapter,
    Annotation,
    BookMetadata,
    ChapterAnalysis,
    ChapterContent,
    ChapterSnapshot,
    Highlight,
    Reflection,
    UserNote,
)
from cove_book_forge.contracts.analysis import (
    Concept,
    DecisionRule,
    EvidenceRef,
    Framework,
    MentalModel,
    Method,
    QualityWarning,
    WorkedExample,
)
from cove_book_forge.contracts.outputs import ObsidianPublishResult
from cove_book_forge.outputs.obsidian_models import ObsidianBookManifest
from cove_book_forge.outputs.obsidian_render import ObsidianRenderer, canonical_manifest_bytes


def _snapshot(
    *, title: str = "Make reversible moves", content: str = "Original chapter text."
) -> ChapterSnapshot:
    return ChapterSnapshot(
        source_system="cove",
        external_book_id="book-42",
        book=BookMetadata(title="The \u00dcber Book", author="Ada", total_chapters=3),
        chapter=ChapterContent(
            index=0, title=title, content=content, source_locator="epub:spine-1"
        ),
        highlights=(Highlight(id="h1", text="Keep options open.", note="Useful reminder."),),
        user_notes=(UserNote(id="n1", text="Try this at work."),),
        annotations=(Annotation(id="a1", text="Question this assumption.", author_label="Reader"),),
        reflections=(
            Reflection(id="r1", text="This changes my next step.", author_label="Reader"),
        ),
    )


def _analyzed(*, fingerprint: str = "a" * 64) -> AnalyzedChapter:
    return AnalyzedChapter(
        input_fingerprint=fingerprint,
        cache_hit=True,
        analysis=ChapterAnalysis(
            core_idea="Choose the smallest reversible action.",
            frameworks=(
                Framework(
                    name="Reversible step",
                    when_to_use="When uncertainty is high.",
                    how=("Reduce scope.", "Observe the result."),
                    why="It limits downside.",
                    limitations=("Not for emergencies.",),
                ),
            ),
            concepts=(
                Concept(
                    term="Option value",
                    definition="Flexibility has value before commitment.",
                    evidence_refs=(EvidenceRef(locator="p. 4", note="Definition"),),
                ),
                Concept(term="Option value", definition="Flexibility has value before commitment."),
            ),
            mental_models=(
                MentalModel(
                    name="Reversibility",
                    explanation="Prefer choices that can be undone.",
                    when_to_use="Early decisions.",
                ),
            ),
            methods=(
                Method(
                    name="Small experiment",
                    steps=("Limit scope.", "Measure."),
                    when_to_use="Before commitment.",
                ),
            ),
            anti_patterns=(
                {"name": "Big bet", "why": "It locks options.", "alternative": "Run a small test."},
            ),
            decision_rules=(
                DecisionRule(
                    rule="When uncertain, choose the reversible option.",
                    conditions=("Uncertainty is material.",),
                ),
                DecisionRule(
                    rule="When uncertain, choose the reversible option.",
                    conditions=("Uncertainty is material.",),
                ),
            ),
            worked_examples=(
                WorkedExample(
                    title="Pilot launch",
                    situation="A new market.",
                    application="Launch to one segment.",
                    result="Learn cheaply.",
                ),
            ),
            key_takeaways=("Keep commitments reversible.",),
            highlight_insights=("The highlight supports a small pilot.",),
            annotation_insights=("The annotation questions the premise.",),
            topic_tags=("decision-making", "experiments"),
            evidence_refs=(EvidenceRef(locator="p. 4", note="Supporting passage"),),
            quality_warnings=(
                QualityWarning(code="EVIDENCE_THIN", message="One claim needs corroboration."),
            ),
        ),
    )


def _render(snapshot: ChapterSnapshot | None = None, analyzed: AnalyzedChapter | None = None):
    return ObsidianRenderer(ObsidianOutputConfig()).render(
        snapshot or _snapshot(), analyzed or _analyzed(), None
    )


def _frontmatter_and_body(data: bytes) -> tuple[dict[str, object], bytes]:
    prefix, body = data.split(b"---\n", 2)[1:]
    fields = {
        line.split(": ", 1)[0]: json.loads(line.split(": ", 1)[1])
        for line in prefix.decode("utf-8").splitlines()
    }
    return fields, body


def test_public_result_and_internal_manifest_are_strict_and_frozen() -> None:
    result = ObsidianPublishResult(
        book_key="0123456789abcdef",
        chapter_path="Books/book/Chapters/01 chapter.md",
        moc_path="Books/book/book MOC.md",
        input_fingerprint="a" * 64,
    )
    with pytest.raises(ValidationError):
        ObsidianPublishResult.model_validate({**result.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        result.book_key = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ObsidianBookManifest.model_validate({"schema": 1, "book_key": "short"})


def test_rendering_uses_stable_identities_across_unicode_and_newline_variants() -> None:
    baseline = _render()
    equivalent = _render(
        _snapshot(title="Make reversible moves", content="Original chapter text.\r\n"),
        _analyzed(),
    )
    assert baseline.manifest.book_key == "4a35674a27a8d10c"
    assert baseline.manifest.book_key == equivalent.manifest.book_key
    assert baseline.card_paths == equivalent.card_paths


def test_card_identity_normalizes_unicode_and_line_endings_in_its_canonical_content() -> None:
    baseline = _analyzed()
    variant_concept = Concept(
        term="Optio\u0301n value",
        definition="Flexibility has value before commitment.",
        evidence_refs=(EvidenceRef(locator="pa\u0301ge 4", note="Definition\r\nwith context"),),
    )
    canonical_concept = Concept(
        term="Opti\u00f3n value",
        definition="Flexibility has value before commitment.",
        evidence_refs=(EvidenceRef(locator="p\u00e1ge 4", note="Definition\nwith context"),),
    )
    canonical = baseline.model_copy(
        update={"analysis": baseline.analysis.model_copy(update={"concepts": (canonical_concept,)})}
    )
    variant = baseline.model_copy(
        update={"analysis": baseline.analysis.model_copy(update={"concepts": (variant_concept,)})}
    )
    assert _render(analyzed=canonical).card_paths == _render(analyzed=variant).card_paths


def test_rendered_relative_paths_stay_within_a_portable_length_bound() -> None:
    rendered = ObsidianRenderer(
        ObsidianOutputConfig(notes_folder="N" * 120, cards_folder="C" * 120)
    ).render(_snapshot(), _analyzed(), None)
    assert all(len(path.encode("utf-8")) <= 240 for path in rendered.files)


def test_title_collisions_only_change_display_paths_not_card_identity() -> None:
    first = _render(_snapshot(title="A/B"))
    second = _render(_snapshot(title="A:B"))
    assert first.chapter_path != second.chapter_path
    assert first.card_paths == second.card_paths
    assert "/" not in first.chapter_path.rsplit("/", 1)[-1]
    assert ":" not in second.chapter_path.rsplit("/", 1)[-1]


def test_rendering_is_deterministic_and_has_only_controlled_json_frontmatter() -> None:
    rendered = _render(
        _snapshot(
            title="---\ncove_evil: true",
            content='---\ncove_injected: true\n<iframe src="https://evil.example">',
        )
    )
    repeated = _render(
        _snapshot(
            title="---\ncove_evil: true",
            content='---\ncove_injected: true\n<iframe src="https://evil.example">',
        )
    )
    assert rendered.files == repeated.files
    expected_fields = {
        "cove_book_forge",
        "cove_schema",
        "cove_kind",
        "cove_book_key",
        "cove_chapter_index",
        "cove_source_fingerprint",
        "cove_stable_id",
        "cove_body_sha256",
    }
    for path, data in rendered.files.items():
        assert path.startswith(("Books/", "Cards/", ".cove-book-forge/"))
        assert "\\" not in path
        if path.endswith(".md"):
            fields, body = _frontmatter_and_body(data)
            assert set(fields) == expected_fields
            assert all(
                json.dumps(value, ensure_ascii=False) in data.decode("utf-8")
                for value in fields.values()
            )
            assert all(
                line.split(": ", 1)[1].startswith('"')
                for line in data.decode("utf-8").split("---\n", 2)[1].splitlines()
            )
            assert fields["cove_body_sha256"] == hashlib.sha256(body).hexdigest()
            assert "<iframe" not in body.decode("utf-8")


def test_chapter_cards_and_moc_cover_the_locked_reading_material() -> None:
    rendered = _render()
    chapter = rendered.files[rendered.chapter_path].decode("utf-8")
    for heading in (
        "## Source",
        "## Core idea",
        "## Frameworks",
        "## Concepts",
        "## Mental models",
        "## Methods",
        "## Anti-patterns",
        "## Decision rules",
        "## Worked examples",
        "## Key takeaways",
        "## Highlights",
        "## User notes",
        "## Annotations and reflections",
        "## Evidence",
        "## Quality warnings",
        "## Cards",
    ):
        assert heading in chapter
    assert "Choose the smallest reversible action." in chapter
    assert (
        "[[" not in chapter
    )  # renderer uses URL-escaped Markdown links, not input-controlled wikilinks
    assert all(path in rendered.files for path in rendered.card_paths)
    assert len(rendered.card_paths) == 4
    for card_path in rendered.card_paths:
        card = rendered.files[card_path].decode("utf-8")
        assert "## Source chapter" in card
        assert "## Book MOC" in card
        assert "## Concept" in card or "## Decision rule" in card
    moc = rendered.files[rendered.moc_path].decode("utf-8")
    for heading in (
        "## Coverage",
        "## Chapter directory",
        "## Frameworks",
        "## Topics",
        "## Cards",
    ):
        assert heading in moc
    assert "Reversible step" in moc
    assert "decision-making" in moc


def test_empty_analysis_collections_render_fixed_sections_without_private_source_or_absolute_paths() -> (
    None
):
    empty = AnalyzedChapter(
        input_fingerprint="b" * 64,
        cache_hit=False,
        analysis=ChapterAnalysis(core_idea="Only the core idea remains."),
    )
    rendered = _render(_snapshot(content="private body /Users/reader/secret"), empty)
    joined = b"".join(rendered.files.values()).decode("utf-8")
    assert "private body" not in joined
    assert "/Users/reader/secret" not in joined
    assert "- None." in joined
    payload = json.loads(canonical_manifest_bytes(rendered.manifest))
    assert "private body" not in json.dumps(payload, ensure_ascii=False)
    assert all(not path.startswith("/") and ".." not in path.split("/") for path in rendered.files)
