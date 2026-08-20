from __future__ import annotations

import json
import os
import stat
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
from cove_book_forge.outputs import ObsidianOutput
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
        raise AssertionError("ChapterAnalyzer must request structured JSON")

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


def _config(data_dir: Path, vault: Path) -> AppConfig:
    return AppConfig.model_validate(
        {
            "library": {"enabled": False, "data_dir": data_dir},
            "model": {"provider": "deterministic-fake", "model": "chapter-model"},
            "outputs": {
                "obsidian": {
                    "enabled": True,
                    "vault_path": vault,
                    "notes_folder": "Books",
                    "cards_folder": "Cards",
                }
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
        database._close_connection()  # noqa: SLF001 - real SQLite integration lifecycle
        if library._books_fd is not None:  # noqa: SLF001 - owned service descriptor
            with suppress(OSError):
                os.close(library._books_fd)  # noqa: SLF001
            library._books_fd = None  # noqa: SLF001
        if library._data_root_fd is not None:  # noqa: SLF001 - owned service descriptor
            with suppress(OSError):
                os.close(library._data_root_fd)  # noqa: SLF001
            library._data_root_fd = None  # noqa: SLF001


def _snapshot(
    index: int = 0,
    *,
    note: str = "Keep the original evidence.",
    book_title: str = "Durable Decisions",
) -> ChapterSnapshot:
    return ChapterSnapshot.model_validate(
        {
            "source_system": "integration-reader",
            "external_book_id": "stable-book-id",
            "book": {"title": book_title, "author": "Reader", "total_chapters": 2},
            "chapter": {
                "index": index,
                "title": f"Chapter {index + 1}",
                "content": f"Complete normalized chapter {index + 1} text.",
                "source_locator": f"reader:chapter:{index}",
            },
            "highlights": [{"id": f"highlight-{index}", "text": "A reversible step."}],
            "user_notes": [{"id": f"note-{index}", "text": note}],
        }
    )


def _analysis(
    *,
    core_idea: str,
    concept: str,
    framework: str,
    topic: str,
) -> dict[str, JsonValue]:
    return {
        "core_idea": core_idea,
        "frameworks": [{"name": framework, "how": ["Inspect", "Decide"]}],
        "concepts": [{"term": concept, "definition": f"Definition of {concept}."}],
        "decision_rules": [{"rule": f"Apply {concept} when evidence is complete."}],
        "topic_tags": [topic],
    }


def _file_state(vault: Path) -> dict[str, tuple[int, int, bytes]]:
    files: dict[str, tuple[int, int, bytes]] = {}
    for path in vault.rglob("*"):
        if path.is_file() and "/.transactions/" not in path.as_posix():
            status = path.stat(follow_symlinks=False)
            files[path.relative_to(vault).as_posix()] = (
                status.st_ino,
                status.st_mtime_ns,
                path.read_bytes(),
            )
    return files


def _filesystem_state(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    for path in (root, *sorted(root.rglob("*"))):
        status = path.lstat()
        if stat.S_ISREG(status.st_mode):
            payload: object = path.read_bytes()
        elif stat.S_ISLNK(status.st_mode):
            payload = path.readlink().as_posix()
        else:
            payload = None
        entries.append(
            (
                "." if path == root else path.relative_to(root).as_posix(),
                status.st_dev,
                status.st_ino,
                status.st_mode,
                status.st_size,
                status.st_mtime_ns,
                status.st_ctime_ns,
                payload,
            )
        )
    return tuple(entries)


@pytest.mark.anyio
async def test_recreated_services_reuse_analysis_without_rewriting_then_update_once(
    tmp_path: Path,
) -> None:
    """Dropping cache reuse or idempotence would charge twice or replace unchanged files."""
    vault = tmp_path / "vault"
    vault.mkdir()
    config = _config(tmp_path / "library", vault)
    snapshot = _snapshot()
    generated_provider = _FakeProvider(
        [
            _analysis(
                core_idea="Start with a reversible decision.",
                concept="Reversibility",
                framework="Reversible loop",
                topic="decisions",
            )
        ]
    )

    with _open_library(config) as library:
        generated = await ChapterAnalyzer(
            generated_provider, library, config.analysis, config.model
        ).analyze(snapshot)
        first = ObsidianOutput(config.outputs.obsidian).publish(snapshot, generated)

    first_state = _file_state(vault)
    zero_call_provider = _FakeProvider([])
    with _open_library(config) as library:
        reused = await ChapterAnalyzer(
            zero_call_provider, library, config.analysis, config.model
        ).analyze(snapshot)
        unchanged = ObsidianOutput(config.outputs.obsidian).publish(snapshot, reused)
        unchanged_state = _file_state(vault)

        changed_snapshot = _snapshot(note="Only this user note changed.")
        changed_provider = _FakeProvider(
            [
                _analysis(
                    core_idea="Re-evaluate after a note changes.",
                    concept="Reversibility",
                    framework="Reversible loop",
                    topic="decisions",
                )
            ]
        )
        changed = await ChapterAnalyzer(
            changed_provider, library, config.analysis, config.model
        ).analyze(changed_snapshot)
        updated = ObsidianOutput(config.outputs.obsidian).publish(changed_snapshot, changed)

    assert generated_provider.calls == 1
    assert zero_call_provider.calls == 0
    assert reused.cache_hit is True
    assert unchanged.unchanged is True
    assert unchanged.changed_paths == ()
    assert unchanged_state == first_state
    updated_state = _file_state(vault)
    assert updated_state != first_state
    assert changed_provider.calls == 1
    manifest_path = f".cove-book-forge/obsidian/{first.book_key}.json"
    assert set(updated.changed_paths) == {
        updated.chapter_path,
        updated.moc_path,
        manifest_path,
        *updated.card_paths,
    }
    for path, state in first_state.items():
        if path not in updated.changed_paths:
            assert updated_state[path] == state


@pytest.mark.anyio
async def test_two_chapters_survive_recreation_and_book_title_change_keeps_root(
    tmp_path: Path,
) -> None:
    """Replacing manifest history would drop the first chapter or move the stable book root."""
    vault = tmp_path / "vault"
    vault.mkdir()
    config = _config(tmp_path / "library", vault)
    provider = _FakeProvider(
        [
            _analysis(
                core_idea="First chapter idea.",
                concept="First concept",
                framework="First framework",
                topic="first-topic",
            ),
            _analysis(
                core_idea="Second chapter idea.",
                concept="Second concept",
                framework="Second framework",
                topic="second-topic",
            ),
        ]
    )
    first_snapshot = _snapshot(0)
    second_snapshot = _snapshot(1)

    with _open_library(config) as library:
        analyzer = ChapterAnalyzer(provider, library, config.analysis, config.model)
        first_analysis = await analyzer.analyze(first_snapshot)
        first = ObsidianOutput(config.outputs.obsidian).publish(first_snapshot, first_analysis)
        second_analysis = await analyzer.analyze(second_snapshot)
        second = ObsidianOutput(config.outputs.obsidian).publish(second_snapshot, second_analysis)

    manifest_path = vault / f".cove-book-forge/obsidian/{first.book_key}.json"
    manifest = json.loads(manifest_path.read_bytes())
    moc = (vault / second.moc_path).read_text(encoding="utf-8")
    assert [chapter["index"] for chapter in manifest["chapters"]] == [0, 1]
    assert manifest["total_chapters"] == 2
    for expected in (
        "Chapter 1",
        "Chapter 2",
        "First framework",
        "Second framework",
        "first-topic",
        "second-topic",
        "First concept",
        "Second concept",
    ):
        assert expected in moc

    old_book_directory = manifest["book_directory"]
    old_moc_path = second.moc_path
    zero_call_provider = _FakeProvider([])
    renamed_snapshot = _snapshot(0, book_title="A New Display Title")
    with _open_library(config) as library:
        renamed_analysis = await ChapterAnalyzer(
            zero_call_provider, library, config.analysis, config.model
        ).analyze(renamed_snapshot)
        renamed = ObsidianOutput(config.outputs.obsidian).publish(
            renamed_snapshot, renamed_analysis
        )

    renamed_manifest = json.loads(manifest_path.read_bytes())
    assert zero_call_provider.calls == 0
    assert renamed_analysis.cache_hit is True
    assert renamed_manifest["book_directory"] == old_book_directory
    assert renamed.moc_path == old_moc_path
    assert [chapter["index"] for chapter in renamed_manifest["chapters"]] == [0, 1]


@pytest.mark.anyio
async def test_skill_placeholder_consumes_cached_analysis_without_creating_skill_files(
    tmp_path: Path,
) -> None:
    """A second output consumer must reuse analysis instead of invoking its own generation."""
    vault = tmp_path / "vault"
    vault.mkdir()
    config = _config(tmp_path / "library", vault)
    snapshot = _snapshot()
    provider = _FakeProvider(
        [
            _analysis(
                core_idea="One analysis feeds multiple consumers.",
                concept="Shared analysis",
                framework="Reuse boundary",
                topic="cache",
            )
        ]
    )

    with _open_library(config) as library:
        analyzer = ChapterAnalyzer(provider, library, config.analysis, config.model)
        ob_analysis = await analyzer.analyze(snapshot)
        ObsidianOutput(config.outputs.obsidian).publish(snapshot, ob_analysis)
        before_placeholder = _filesystem_state(tmp_path)
        skill_cached = await analyzer.analyze(snapshot)
        skill_placeholder = skill_cached.analysis
        after_placeholder = _filesystem_state(tmp_path)

    assert provider.calls == 1
    assert skill_cached.cache_hit is True
    assert skill_placeholder == ob_analysis.analysis
    assert after_placeholder == before_placeholder
    assert not any(path.name == "SKILL.md" for path in tmp_path.rglob("*"))
