from __future__ import annotations

import json
from pathlib import Path

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
from cove_book_forge.outputs import ObsidianOutput


def _snapshot(*, title: str = "Chapter one") -> ChapterSnapshot:
    return ChapterSnapshot(
        source_system="publisher-tests",
        external_book_id="book-123",
        book=BookMetadata(title="Safe publishing", author="A. Author", total_chapters=2),
        chapter=ChapterContent(index=0, title=title, content="Private source body."),
    )


def _analyzed(
    *, fingerprint: str = "a" * 64, concepts: tuple[Concept, ...] = ()
) -> AnalyzedChapter:
    return AnalyzedChapter(
        input_fingerprint=fingerprint,
        cache_hit=True,
        analysis=ChapterAnalysis(
            core_idea="Publish a complete bundle.",
            concepts=concepts,
        ),
    )


def _output(vault: Path, *, enabled: bool = True) -> ObsidianOutput:
    return ObsidianOutput(ObsidianOutputConfig(enabled=enabled, vault_path=vault))


def _visible_files(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file() and "/.transactions/" not in path.as_posix()
    }


def _assert_safe_error(error: ForgeException, vault: Path) -> None:
    public = repr(error) + str(error) + json.dumps(error.as_result(), ensure_ascii=False)
    assert str(vault) not in public
    assert "Private source body" not in public
    assert "Traceback" not in public
    assert error.__cause__ is None


def test_disabled_output_fails_before_rendering_or_writing(tmp_path: Path) -> None:
    vault = tmp_path / "missing-vault"

    with pytest.raises(ForgeException) as raised:
        _output(vault, enabled=False).publish(_snapshot(), _analyzed())

    assert raised.value.code is ForgeErrorCode.OUTPUT_NOT_CONFIGURED
    assert not vault.exists()
    _assert_safe_error(raised.value, vault)


@pytest.mark.parametrize("kind", ["missing", "file", "symlink"])
def test_output_requires_an_existing_real_directory(tmp_path: Path, kind: str) -> None:
    vault = tmp_path / "vault"
    if kind == "file":
        vault.write_bytes(b"not a directory")
    elif kind == "symlink":
        real = tmp_path / "real"
        real.mkdir()
        vault.symlink_to(real, target_is_directory=True)

    with pytest.raises(ForgeException) as raised:
        _output(vault).publish(_snapshot(), _analyzed())

    assert raised.value.code in {
        ForgeErrorCode.OUTPUT_NOT_CONFIGURED,
        ForgeErrorCode.PATH_NOT_ALLOWED,
    }
    assert not (tmp_path / "real" / "Books").exists()
    _assert_safe_error(raised.value, vault)


def test_first_publish_is_deterministic_and_second_publish_does_not_rewrite(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    output = _output(vault)

    first = output.publish(_snapshot(), _analyzed())
    mtimes = {
        path: (vault / path).stat().st_mtime_ns
        for path in (first.chapter_path, first.moc_path, *first.card_paths)
    }
    second = output.publish(_snapshot(), _analyzed())

    assert first.unchanged is False
    assert first.changed_paths == tuple(sorted(first.changed_paths))
    assert set(first.changed_paths) == {
        first.chapter_path,
        first.moc_path,
        f".cove-book-forge/obsidian/{first.book_key}.json",
    }
    assert (
        second.model_copy(update={"unchanged": False, "changed_paths": first.changed_paths})
        == first
    )
    assert second.unchanged is True
    assert second.changed_paths == ()
    assert mtimes == {path: (vault / path).stat().st_mtime_ns for path in mtimes}
    assert not (vault / ".cove-book-forge" / ".transactions").exists()


def test_update_removes_a_stale_card_and_reports_exact_sorted_paths(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    output = _output(vault)
    first = output.publish(
        _snapshot(),
        _analyzed(concepts=(Concept(term="Old card", definition="Old definition."),)),
    )
    assert len(first.card_paths) == 1

    updated = output.publish(_snapshot(), _analyzed(fingerprint="b" * 64))

    manifest_path = f".cove-book-forge/obsidian/{first.book_key}.json"
    assert updated.changed_paths == tuple(sorted(updated.changed_paths))
    assert set(updated.changed_paths) == {
        first.card_paths[0],
        first.chapter_path,
        first.moc_path,
        manifest_path,
    }
    assert not (vault / first.card_paths[0]).exists()
    assert (vault / updated.chapter_path).is_file()


def test_external_edit_and_manifest_deletion_never_get_adopted(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    output = _output(vault)
    first = output.publish(_snapshot(), _analyzed())
    chapter = vault / first.chapter_path
    chapter.write_bytes(chapter.read_bytes() + b"\nprivate edit")

    with pytest.raises(ForgeException) as raised:
        output.publish(_snapshot(), _analyzed(fingerprint="b" * 64))

    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert chapter.read_bytes().endswith(b"private edit")
    _assert_safe_error(raised.value, vault)

    chapter.write_bytes(chapter.read_bytes().removesuffix(b"\nprivate edit"))
    (vault / f".cove-book-forge/obsidian/{first.book_key}.json").unlink()
    with pytest.raises(ForgeException) as missing:
        output.publish(_snapshot(), _analyzed(fingerprint="c" * 64))
    assert missing.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION


def test_source_never_selects_a_path_outside_the_renderer_contract(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    output = _output(vault)
    outside = tmp_path / "outside.md"

    result = output.publish(_snapshot(title="../../outside.md"), _analyzed())

    assert all(".." not in path.split("/") for path in result.changed_paths)
    assert not outside.exists()
