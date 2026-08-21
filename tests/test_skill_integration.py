from __future__ import annotations

import errno
import os
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path

import pytest
from pydantic import JsonValue

from cove_book_forge.analysis import ChapterAnalyzer
from cove_book_forge.config import AppConfig
from cove_book_forge.contracts import ChapterSnapshot
from cove_book_forge.library import BookLibrary, LibraryDatabase, LibraryRepository
from cove_book_forge.outputs import AgentSkillOutput, ObsidianOutput
from cove_book_forge.outputs import skill_install as skill_install_module
from cove_book_forge.providers import (
    JsonGeneration,
    ProviderCapabilities,
    ProviderUsage,
    TextGeneration,
)

pytestmark = pytest.mark.filterwarnings("error::ResourceWarning")


class _FakeProvider:
    def __init__(self, responses: list[dict[str, JsonValue]]) -> None:
        self._responses: deque[dict[str, JsonValue]] = deque(responses)
        self.calls = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(json_mode=True)

    @property
    def usage(self) -> ProviderUsage:
        return ProviderUsage()

    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
    ) -> TextGeneration:
        raise AssertionError("structured analysis is required")

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
        json_schema: Mapping[str, JsonValue] | None = None,
    ) -> JsonGeneration:
        del system_prompt, user_prompt, max_output_tokens, temperature, json_schema
        self.calls += 1
        return JsonGeneration(
            value=self._responses.popleft(),
            model="deterministic-fake",
            usage=ProviderUsage(),
        )

    async def healthcheck(self) -> None:
        return None


def _config(data_dir: Path, vault: Path, canonical: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "library": {"enabled": False, "data_dir": data_dir},
            "model": {"provider": "deterministic-fake", "model": "chapter-model"},
            "outputs": {
                "obsidian": {"enabled": True, "vault_path": vault},
                "skills": {
                    "enabled": True,
                    "canonical_path": canonical,
                    "install_to": ["codex"],
                },
            },
        }
    )


@contextmanager
def _open_library(config: AppConfig) -> Iterator[BookLibrary]:
    database = LibraryDatabase(config.library.data_dir / "library.sqlite3")
    library = BookLibrary(config, repository=LibraryRepository(database))
    library.initialize()
    try:
        yield library
    finally:
        database._close_connection()  # noqa: SLF001 - real SQLite lifecycle
        if library._books_fd is not None:  # noqa: SLF001
            with suppress(OSError):
                os.close(library._books_fd)  # noqa: SLF001
            library._books_fd = None  # noqa: SLF001
        if library._data_root_fd is not None:  # noqa: SLF001
            with suppress(OSError):
                os.close(library._data_root_fd)  # noqa: SLF001
            library._data_root_fd = None  # noqa: SLF001


def _snapshot(*, note: str = "Keep the evidence.") -> ChapterSnapshot:
    return ChapterSnapshot.model_validate(
        {
            "source_system": "integration-reader",
            "external_book_id": "stable-book-id",
            "book": {"title": "Durable Decisions", "total_chapters": 1},
            "chapter": {
                "index": 0,
                "title": "Reversible moves",
                "content": "Complete normalized text.",
                "source_locator": "reader:chapter:0",
            },
            "user_notes": [{"id": "note-0", "text": note}],
        }
    )


def _analysis(core_idea: str) -> dict[str, JsonValue]:
    return {
        "core_idea": core_idea,
        "concepts": [{"term": "Reversibility", "definition": "Keep options open."}],
        "topic_tags": ["decisions"],
    }


@pytest.mark.anyio
@pytest.mark.parametrize("first_output", ("obsidian", "skill"))
async def test_outputs_share_one_persistent_analysis_and_copy_updates_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_output: str,
) -> None:
    """Calling a Provider per output, rewriting cache hits, or leaving a stale copy must fail."""
    home = tmp_path / "home"
    (home / ".codex/skills").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    vault = tmp_path / "vault"
    canonical = tmp_path / "canonical"
    vault.mkdir()
    canonical.mkdir()
    config = _config(tmp_path / "library", vault, canonical)

    def unsupported(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "symlinks unsupported")

    monkeypatch.setattr(skill_install_module, "_create_symlink", unsupported)
    snapshot = _snapshot()
    provider = _FakeProvider([_analysis("Start with a reversible decision.")])
    with _open_library(config) as library:
        analyzed = await ChapterAnalyzer(provider, library, config.analysis, config.model).analyze(
            snapshot
        )
        outputs = {
            "obsidian": lambda: ObsidianOutput(config.outputs.obsidian).publish(snapshot, analyzed),
            "skill": lambda: AgentSkillOutput(config.outputs.skills).publish(snapshot, analyzed),
        }
        outputs[first_output]()
        outputs["skill" if first_output == "obsidian" else "obsidian"]()

    assert provider.calls == 1
    installed = next((home / ".codex/skills").iterdir())
    initial_manifest = (installed / ".cove-book-forge.json").read_bytes()

    zero_provider = _FakeProvider([])
    with _open_library(config) as library:
        reused = await ChapterAnalyzer(
            zero_provider, library, config.analysis, config.model
        ).analyze(snapshot)
        obsidian_unchanged = ObsidianOutput(config.outputs.obsidian).publish(snapshot, reused)
        skill_unchanged = AgentSkillOutput(config.outputs.skills).publish(snapshot, reused)

        changed_snapshot = _snapshot(note="This note changed.")
        changed_provider = _FakeProvider([_analysis("Re-evaluate the reversible decision.")])
        changed = await ChapterAnalyzer(
            changed_provider, library, config.analysis, config.model
        ).analyze(changed_snapshot)
        changed_obsidian = ObsidianOutput(config.outputs.obsidian).publish(
            changed_snapshot, changed
        )
        changed_skill = AgentSkillOutput(config.outputs.skills).publish(changed_snapshot, changed)

    assert zero_provider.calls == 0
    assert reused.cache_hit is True
    assert obsidian_unchanged.unchanged is True
    assert skill_unchanged.unchanged is True
    assert changed_provider.calls == 1
    assert changed_obsidian.unchanged is False
    assert changed_skill.unchanged is False
    assert changed_skill.installations[0].strategy == "copy"
    assert changed_skill.installations[0].unchanged is False
    assert (installed / ".cove-book-forge.json").read_bytes() != initial_manifest
