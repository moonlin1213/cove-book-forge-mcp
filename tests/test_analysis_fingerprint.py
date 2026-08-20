import hashlib
import json

import pytest
from pydantic import ValidationError

from cove_book_forge.analysis.fingerprint import (
    canonical_analysis_source_payload,
    canonical_chapter_input_payload,
    chapter_input_fingerprint,
)
from cove_book_forge.config.models import AnalysisConfig, ModelConfig
from cove_book_forge.contracts import ChapterAnalysis, ChapterSnapshot
from cove_book_forge.contracts.analysis import AnalyzedChapter


def _snapshot(**changes: object) -> ChapterSnapshot:
    payload: dict[str, object] = {
        "source_system": "fixture",
        "external_book_id": "book-7",
        "book": {"title": "Ignored metadata", "author": "Author"},
        "chapter": {
            "index": 3,
            "title": "Cafe\u0301\r\nTitle",
            "content": "Line one\rLine two\r\n第三行",
            "source_locator": "epub:3",
        },
        "highlights": [
            {"id": "highlight-b", "text": "second"},
            {"id": "highlight-a", "text": "first"},
        ],
        "user_notes": [
            {"id": "note-b", "text": "note two"},
            {"id": "note-a", "text": "note one"},
        ],
        "annotations": [
            {"id": "annotation-b", "text": "remark two"},
            {"id": "annotation-a", "text": "remark one"},
        ],
        "reflections": [
            {"id": "reflection-b", "text": "reflection two"},
            {"id": "reflection-a", "text": "reflection one"},
        ],
    }
    payload.update(changes)
    return ChapterSnapshot.model_validate(payload)


def _model(**changes: object) -> ModelConfig:
    payload: dict[str, object] = {
        "provider": "openai-compatible",
        "model": "model-a",
        "base_url": "https://models.example.test/v1",
        "api_key_env": "MODEL_A_KEY",
    }
    payload.update(changes)
    return ModelConfig.model_validate(payload)


def test_analyzed_chapter_is_a_strict_frozen_public_result() -> None:
    result = AnalyzedChapter(
        analysis=ChapterAnalysis(core_idea="Prefer a small reversible step."),
        input_fingerprint="a" * 64,
        cache_hit=False,
    )
    with pytest.raises(ValidationError):
        AnalyzedChapter.model_validate(
            {
                "analysis": {"core_idea": "Idea"},
                "input_fingerprint": "a" * 64,
                "cache_hit": False,
                "unexpected": "field",
            }
        )
    with pytest.raises(ValidationError):
        result.cache_hit = True  # type: ignore[misc]
    with pytest.raises(ValidationError):
        AnalyzedChapter.model_validate(
            {
                "analysis": ChapterAnalysis(core_idea="Idea"),
                "input_fingerprint": "a" * 64,
                "cache_hit": 1,
            }
        )


def test_fingerprint_is_the_lowercase_sha256_of_canonical_bytes() -> None:
    payload = canonical_chapter_input_payload(_snapshot(), AnalysisConfig(), _model())
    canonical_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")

    fingerprint = chapter_input_fingerprint(_snapshot(), AnalysisConfig(), _model())

    assert fingerprint == hashlib.sha256(canonical_bytes).hexdigest()
    assert len(fingerprint) == 64
    assert fingerprint == fingerprint.lower()


def test_fingerprint_normalizes_line_endings_and_unicode_nfc() -> None:
    normalized = _snapshot()
    equivalent = _snapshot(
        chapter={
            "index": 3,
            "title": "Caf\u00e9\nTitle",
            "content": "Line one\nLine two\n第三行",
            "source_locator": "a different locator",
        }
    )

    assert chapter_input_fingerprint(normalized, AnalysisConfig(), _model()) == chapter_input_fingerprint(
        equivalent, AnalysisConfig(), _model()
    )


def test_order_only_changes_to_supplemental_items_do_not_change_the_fingerprint() -> None:
    snapshot = _snapshot()
    reordered = _snapshot(
        highlights=list(reversed(snapshot.highlights)),
        user_notes=list(reversed(snapshot.user_notes)),
        annotations=list(reversed(snapshot.annotations)),
        reflections=list(reversed(snapshot.reflections)),
    )

    assert chapter_input_fingerprint(snapshot, AnalysisConfig(), _model()) == chapter_input_fingerprint(
        reordered, AnalysisConfig(), _model()
    )


def test_equivalent_supplemental_ids_have_a_total_canonical_sort_order() -> None:
    first = _snapshot(
        highlights=[
            {"id": "Cafe\u0301", "text": "first variant"},
            {"id": "Caf\u00e9", "text": "second variant"},
        ]
    )
    reversed_items = _snapshot(
        highlights=[
            {"id": "Caf\u00e9", "text": "second variant"},
            {"id": "Cafe\u0301", "text": "first variant"},
        ]
    )

    assert canonical_analysis_source_payload(first) == canonical_analysis_source_payload(reversed_items)
    assert chapter_input_fingerprint(first, AnalysisConfig(), _model()) == chapter_input_fingerprint(
        reversed_items, AnalysisConfig(), _model()
    )


@pytest.mark.parametrize(
    "changed_snapshot,changed_config",
    [
        ("title", None),
        ("content", None),
        ("note", None),
        (None, AnalysisConfig(prompt_template_version="chapter-analysis-v2")),
        (None, AnalysisConfig(generator_version="cove-analysis-v2")),
    ],
)
def test_relevant_content_and_analysis_configuration_changes_invalidate_fingerprint(
    changed_snapshot: str | None, changed_config: AnalysisConfig | None
) -> None:
    baseline = _snapshot()
    changed_payload = baseline.model_dump(mode="python")
    if changed_snapshot == "title":
        changed_payload["chapter"] = {**changed_payload["chapter"], "title": "New title"}  # type: ignore[index]
    elif changed_snapshot == "content":
        changed_payload["chapter"] = {**changed_payload["chapter"], "content": "New body"}  # type: ignore[index]
    elif changed_snapshot == "note":
        changed_payload["user_notes"] = [{"id": "note-a", "text": "changed note"}]
    changed = ChapterSnapshot.model_validate(changed_payload)

    assert chapter_input_fingerprint(baseline, AnalysisConfig(), _model()) != chapter_input_fingerprint(
        changed, changed_config or AnalysisConfig(), _model()
    )


def test_schema_change_in_canonical_payload_invalidates_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    baseline = chapter_input_fingerprint(_snapshot(), AnalysisConfig(), _model())
    original = ChapterAnalysis.model_json_schema

    def changed_schema() -> dict[str, object]:
        schema = original()
        return {**schema, "title": "A changed chapter analysis schema"}

    monkeypatch.setattr(ChapterAnalysis, "model_json_schema", changed_schema)

    assert chapter_input_fingerprint(_snapshot(), AnalysisConfig(), _model()) != baseline


def test_provider_identity_only_affects_opted_in_fingerprints() -> None:
    initial = _model()
    changed = _model(provider="anthropic", model="claude", base_url="https://api.anthropic.com")

    assert chapter_input_fingerprint(_snapshot(), AnalysisConfig(), initial) == chapter_input_fingerprint(
        _snapshot(), AnalysisConfig(), changed
    )
    opted_in = AnalysisConfig(include_provider_in_fingerprint=True)
    assert chapter_input_fingerprint(_snapshot(), opted_in, initial) != chapter_input_fingerprint(
        _snapshot(), opted_in, changed
    )


@pytest.mark.parametrize(
    "model_change",
    [
        {"provider": "anthropic"},
        {"model": "model-b"},
        {"base_url": "https://other.example.test/v1"},
    ],
)
def test_each_provider_identity_part_only_invalidates_an_opted_in_fingerprint(
    model_change: dict[str, str]
) -> None:
    baseline = _model()
    changed = _model(**model_change)
    opted_in = AnalysisConfig(include_provider_in_fingerprint=True)

    assert chapter_input_fingerprint(_snapshot(), AnalysisConfig(), baseline) == chapter_input_fingerprint(
        _snapshot(), AnalysisConfig(), changed
    )
    assert chapter_input_fingerprint(_snapshot(), opted_in, baseline) != chapter_input_fingerprint(
        _snapshot(), opted_in, changed
    )


def test_api_key_environment_name_never_affects_or_appears_in_canonical_payload() -> None:
    model = _model()
    renamed_key = _model(api_key_env="A_DIFFERENT_KEY")
    opted_in = AnalysisConfig(include_provider_in_fingerprint=True)
    payload = canonical_chapter_input_payload(_snapshot(), opted_in, model)
    serialized = json.dumps(payload, ensure_ascii=False)

    assert chapter_input_fingerprint(_snapshot(), opted_in, model) == chapter_input_fingerprint(
        _snapshot(), opted_in, renamed_key
    )
    assert "MODEL_A_KEY" not in serialized
    assert "A_DIFFERENT_KEY" not in serialized


@pytest.mark.parametrize("field", ["highlights", "user_notes", "annotations", "reflections"])
def test_each_supplemental_collection_invalidates_the_fingerprint_when_its_content_changes(
    field: str,
) -> None:
    baseline = _snapshot()
    changed_payload = baseline.model_dump(mode="python")
    changed_payload[field] = [{"id": f"{field}-changed", "text": "changed"}]
    changed = ChapterSnapshot.model_validate(changed_payload)

    assert chapter_input_fingerprint(baseline, AnalysisConfig(), _model()) != chapter_input_fingerprint(
        changed, AnalysisConfig(), _model()
    )


def test_max_chunk_characters_invalidates_the_fingerprint() -> None:
    snapshot = _snapshot()

    assert chapter_input_fingerprint(
        snapshot, AnalysisConfig(max_chunk_characters=128), _model()
    ) != chapter_input_fingerprint(snapshot, AnalysisConfig(max_chunk_characters=1_000_000), _model())
