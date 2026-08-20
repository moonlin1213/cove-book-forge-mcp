"""Portable relative-path validation shared by pure configuration and contracts."""

from __future__ import annotations

import unicodedata

MAX_COMPONENT_BYTES = 120
MAX_PATH_BYTES = 240
_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def validate_relative_path(value: str, *, max_path_bytes: int = MAX_PATH_BYTES) -> str:
    """Normalize and return a portable, non-empty relative POSIX path or raise ValueError."""
    normalized = unicodedata.normalize("NFC", value)
    if not normalized or normalized.startswith(("/", "\\")) or "\\" in normalized:
        raise ValueError("path must be a safe relative POSIX path")
    if len(normalized.encode("utf-8")) > max_path_bytes:
        raise ValueError("path exceeds the portable byte limit")
    for component in normalized.split("/"):
        _validate_component(component)
    return normalized


def validate_component(value: str, *, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    normalized = unicodedata.normalize("NFC", value)
    _validate_component(normalized, max_bytes=max_bytes)
    return normalized


def _validate_component(component: str, *, max_bytes: int = MAX_COMPONENT_BYTES) -> None:
    stem = component.split(".", 1)[0].casefold()
    if (
        not component
        or component in {".", ".."}
        or component.strip() != component
        or component.endswith((".", " "))
        or len(component.encode("utf-8")) > max_bytes
        or stem in _RESERVED
        or any(unicodedata.category(character).startswith("C") for character in component)
        or any(character in ':*?"<>|' for character in component)
    ):
        raise ValueError("path must use portable safe components")
