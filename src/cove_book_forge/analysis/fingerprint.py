"""Canonical, secret-free chapter-analysis cache inputs."""

import hashlib
import json
import unicodedata
from typing import Any

from cove_book_forge.config.models import AnalysisConfig, ModelConfig
from cove_book_forge.contracts.analysis import ChapterAnalysis
from cove_book_forge.contracts.books import ChapterSnapshot
from cove_book_forge.providers.routes import canonical_provider_route_identity


def chapter_input_fingerprint(
    snapshot: ChapterSnapshot,
    analysis_config: AnalysisConfig,
    model_config: ModelConfig,
) -> str:
    """Return the stable SHA-256 input identity for one chapter analysis."""
    canonical_bytes = _canonical_json_bytes(
        canonical_chapter_input_payload(snapshot, analysis_config, model_config)
    )
    return hashlib.sha256(canonical_bytes).hexdigest()


def canonical_chapter_input_payload(
    snapshot: ChapterSnapshot,
    analysis_config: AnalysisConfig,
    model_config: ModelConfig,
) -> dict[str, Any]:
    """Build the secret-free normalized payload used by the fingerprint."""
    payload: dict[str, Any] = {
        "analysis_config": _normalize_value(analysis_config.model_dump(mode="json")),
        "chapter_analysis_schema_sha256": hashlib.sha256(
            _canonical_json_bytes(ChapterAnalysis.model_json_schema())
        ).hexdigest(),
        "source": canonical_analysis_source_payload(snapshot),
    }
    if analysis_config.include_provider_in_fingerprint:
        payload["provider"] = {
            "model": _normalize_text(model_config.model),
            "provider": _normalize_text(model_config.provider),
            "route": canonical_provider_route_identity(model_config),
        }
    return payload


def canonical_analysis_source_payload(snapshot: ChapterSnapshot) -> dict[str, Any]:
    """Return the complete normalized source data that analysis may consume."""
    return {
        "chapter": {
            "content": _normalize_text(snapshot.chapter.content),
            "title": _normalize_text(snapshot.chapter.title),
        },
        "highlights": _sorted_items(snapshot.highlights),
        "user_notes": _sorted_items(snapshot.user_notes),
        "annotations": _sorted_items(snapshot.annotations),
        "reflections": _sorted_items(snapshot.reflections),
    }


def _sorted_items(items: tuple[Any, ...]) -> list[dict[str, Any]]:
    normalized = [_normalize_value(item.model_dump(mode="json")) for item in items]
    return sorted(
        normalized,
        key=lambda item: (item["id"], _canonical_json_bytes(item)),
    )


def _normalize_value(value: Any) -> Any:
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize_value(item) for item in value]
    return value


def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
