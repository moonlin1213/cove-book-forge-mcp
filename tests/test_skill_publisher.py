"""Filesystem, atomicity, and restart tests for canonical Agent Skill publication."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from test_skill_render import _analyzed, _render, _snapshot

from cove_book_forge.config import SkillOutputConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs import AgentSkillRenderer
from cove_book_forge.outputs.skill_managed import validate_skill_bundle
from cove_book_forge.outputs.skill_models import RenderedAgentSkill
from cove_book_forge.outputs.skill_publisher import CanonicalSkillPublisher


class SimulatedProcessCrash(BaseException):
    pass


def _publisher(root: Path) -> CanonicalSkillPublisher:
    return CanonicalSkillPublisher(SkillOutputConfig(enabled=True, canonical_path=root))


def _next(
    previous: RenderedAgentSkill, *, chapter_index: int = 1, title: str = "Second chapter"
) -> RenderedAgentSkill:
    return AgentSkillRenderer(SkillOutputConfig()).render(
        _snapshot(chapter_index=chapter_index, title=title),
        _analyzed(fingerprint=f"{chapter_index + 1:x}" * 64),
        previous.manifest,
    )


def _active(root: Path, rendered: RenderedAgentSkill) -> Path:
    path = root / rendered.skill_slug
    assert path.is_symlink()
    target = os.readlink(path)
    assert not os.path.isabs(target)
    assert ".." not in Path(target).parts
    return path


def _active_files(root: Path, rendered: RenderedAgentSkill) -> dict[str, bytes]:
    active = _active(root, rendered)
    files: dict[str, bytes] = {}
    for path in rendered.manifest.files:
        candidate = active / path.path
        assert stat.S_ISREG(os.lstat(candidate).st_mode)
        files[path.path] = candidate.read_bytes()
    manifest = active / ".cove-book-forge.json"
    assert stat.S_ISREG(os.lstat(manifest).st_mode)
    files[manifest.name] = manifest.read_bytes()
    return files


def _assert_error(error: pytest.ExceptionInfo[ForgeException], code: ForgeErrorCode) -> None:
    assert error.value.code is code
    assert str(error.value) in {
        "Output is not configured.",
        "Output changed outside this application.",
        "Output location is not writable.",
        "Path is outside the authorized locations.",
    }
    assert error.value.details == {}


def test_first_publication_activates_one_relative_managed_generation(tmp_path: Path) -> None:
    rendered = _render()
    receipt = _publisher(tmp_path).publish(rendered)

    assert receipt.rendered is rendered
    assert receipt.canonical_path == rendered.skill_slug
    assert set(receipt.changed_paths) == set(rendered.files)
    assert not receipt.unchanged
    files = _active_files(tmp_path, rendered)
    assert validate_skill_bundle(files) == rendered.manifest
    target = os.readlink(tmp_path / rendered.skill_slug)
    assert target.startswith(f".cove-book-forge/generations/{rendered.manifest.book_key}/gen-")
    assert target.endswith("/content")


def test_unchanged_publication_performs_zero_canonical_rewrites(tmp_path: Path) -> None:
    rendered = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(rendered)
    active = _active(tmp_path, rendered)
    pointer_before = os.lstat(active)
    target_before = os.readlink(active)
    file_before = os.lstat(active / "SKILL.md")
    generations_before = tuple(
        (tmp_path / ".cove-book-forge" / "generations" / rendered.manifest.book_key).iterdir()
    )

    receipt = publisher.publish(rendered)

    assert receipt.unchanged
    assert receipt.changed_paths == ()
    assert os.lstat(active).st_ino == pointer_before.st_ino
    assert os.lstat(active / "SKILL.md").st_ino == file_before.st_ino
    assert os.readlink(active) == target_before
    assert (
        tuple(
            (tmp_path / ".cove-book-forge" / "generations" / rendered.manifest.book_key).iterdir()
        )
        == generations_before
    )


def test_incremental_generation_preserves_history_and_title_change_slug_lock(
    tmp_path: Path,
) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    historical = (tmp_path / first.skill_slug / first.chapter_path).read_bytes()
    second = _next(first, title="A renamed book chapter")

    receipt = publisher.publish(second)

    assert not receipt.unchanged
    assert second.skill_slug == first.skill_slug
    files = _active_files(tmp_path, second)
    assert files[first.chapter_path] == historical
    assert files[second.chapter_path] == second.files[second.chapter_path]
    assert validate_skill_bundle(files) == second.manifest


@pytest.mark.parametrize("occupied_kind", ["file", "directory", "symlink"])
def test_first_publication_never_overwrites_an_occupied_nonmanaged_slug(
    tmp_path: Path, occupied_kind: str
) -> None:
    rendered = _render()
    occupied = tmp_path / rendered.skill_slug
    if occupied_kind == "file":
        occupied.write_bytes(b"competitor")
    elif occupied_kind == "directory":
        occupied.mkdir()
        (occupied / "competitor.txt").write_bytes(b"competitor")
    else:
        (tmp_path / "competitor").mkdir()
        occupied.symlink_to("competitor")

    with pytest.raises(ForgeException) as error:
        _publisher(tmp_path).publish(rendered)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    if occupied_kind == "file":
        assert occupied.read_bytes() == b"competitor"
    elif occupied_kind == "directory":
        assert (occupied / "competitor.txt").read_bytes() == b"competitor"
    else:
        assert os.readlink(occupied) == "competitor"


@pytest.mark.parametrize("mutation", ["missing", "tampered", "symlink"])
def test_existing_managed_generation_modification_fails_closed(
    tmp_path: Path, mutation: str
) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    chapter = tmp_path / first.skill_slug / first.chapter_path
    if mutation == "missing":
        chapter.unlink()
    elif mutation == "tampered":
        chapter.write_bytes(chapter.read_bytes() + b"tampered")
    else:
        chapter.unlink()
        chapter.symlink_to("../SKILL.md")

    with pytest.raises(ForgeException) as error:
        publisher.publish(_next(first))

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)


def test_relative_managed_pointer_cannot_be_redirected_outside_or_to_another_book(
    tmp_path: Path,
) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    active = tmp_path / first.skill_slug
    active.unlink()
    active.symlink_to("../outside")

    with pytest.raises(ForgeException) as error:
        publisher.publish(_next(first))

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert os.readlink(active) == "../outside"


def test_concurrent_pointer_precondition_change_is_not_overwritten_or_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _render()
    second = _next(first)
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    competitor = tmp_path / "competitor"
    competitor.mkdir()
    (competitor / "identity.txt").write_text("competitor", encoding="utf-8")
    changed = False

    def race(phase: str) -> None:
        nonlocal changed
        if phase == "activation:exchange" and not changed:
            active = tmp_path / first.skill_slug
            active.unlink()
            active.symlink_to("competitor")
            changed = True

    monkeypatch.setattr(publisher, "_checkpoint", race)
    with pytest.raises(ForgeException) as error:
        publisher.publish(second)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert os.readlink(tmp_path / first.skill_slug) == "competitor"
    assert (competitor / "identity.txt").read_text(encoding="utf-8") == "competitor"


def test_ordinary_stage_failure_is_cleaned_without_touching_the_active_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _render()
    second = _next(first)
    publisher = _publisher(tmp_path)
    publisher.publish(first)

    def fail(phase: str) -> None:
        if phase == "stage:file":
            raise OSError("injected stage failure")

    monkeypatch.setattr(publisher, "_checkpoint", fail)
    with pytest.raises(ForgeException) as error:
        publisher.publish(second)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert validate_skill_bundle(_active_files(tmp_path, first)) == first.manifest
    transactions = tmp_path / ".cove-book-forge" / "transactions"
    assert list(transactions.iterdir()) == []


def test_root_identity_change_before_activation_fails_without_writing_replacement_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "canonical"
    root.mkdir()
    first = _render()
    second = _next(first)
    publisher = _publisher(root)
    publisher.publish(first)
    moved = tmp_path / "moved-canonical"
    changed = False

    def swap_root(phase: str) -> None:
        nonlocal changed
        if phase == "activation:before" and not changed:
            root.rename(moved)
            root.mkdir()
            changed = True

    monkeypatch.setattr(publisher, "_checkpoint", swap_root)
    with pytest.raises(ForgeException) as error:
        publisher.publish(second)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert not any(root.iterdir())
    assert _active_files(moved, first)[first.chapter_path] == first.files[first.chapter_path]


@pytest.mark.parametrize(
    ("phase", "expected_generation"),
    [
        ("stage:start", "old"),
        ("stage:file", "old"),
        ("activation:before", "old"),
        ("activation:after", "new"),
        ("manifest:switch", "new"),
        ("cleanup:start", "new"),
    ],
)
def test_process_interruption_leaves_a_complete_generation_and_restart_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    expected_generation: str,
) -> None:
    first = _render()
    second = _next(first)
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    crashed = False

    def crash(checkpoint: str) -> None:
        nonlocal crashed
        if checkpoint == phase and not crashed:
            crashed = True
            raise SimulatedProcessCrash(checkpoint)

    monkeypatch.setattr(publisher, "_checkpoint", crash)
    with pytest.raises(SimulatedProcessCrash, match=phase):
        publisher.publish(second)

    visible = _active_files(tmp_path, first if expected_generation == "old" else second)
    assert validate_skill_bundle(visible) == (
        first.manifest if expected_generation == "old" else second.manifest
    )

    recovered = _publisher(tmp_path).publish(second)
    assert validate_skill_bundle(_active_files(tmp_path, second)) == second.manifest
    assert recovered.unchanged is (expected_generation == "new")
    transactions = tmp_path / ".cove-book-forge" / "transactions"
    assert list(transactions.iterdir()) == []


def test_restart_never_adopts_or_deletes_unowned_transaction_debris(tmp_path: Path) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    competitor = tmp_path / ".cove-book-forge" / "transactions" / f"tx-{'f' * 32}"
    competitor.mkdir()
    (competitor / "competitor.txt").write_text("keep", encoding="utf-8")

    receipt = publisher.publish(first)

    assert receipt.unchanged
    assert (competitor / "competitor.txt").read_text(encoding="utf-8") == "keep"


def test_cleanup_keeps_current_and_one_complete_previous_generation(tmp_path: Path) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    second = _next(first)
    publisher.publish(second)
    third = _next(second, chapter_index=2, title="Third chapter")
    publisher.publish(third)

    book_generations = tmp_path / ".cove-book-forge" / "generations" / first.manifest.book_key
    generations = tuple(book_generations.iterdir())
    assert len(generations) == 2
    assert validate_skill_bundle(_active_files(tmp_path, third)) == third.manifest


def test_cleanup_race_never_deletes_a_competitor_inserted_into_an_old_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    first_generation = tmp_path / os.readlink(tmp_path / first.skill_slug)
    second = _next(first)
    publisher.publish(second)
    third = _next(second, chapter_index=2, title="Third chapter")
    inserted = False

    def race(phase: str) -> None:
        nonlocal inserted
        if phase == "cleanup:generation" and not inserted:
            (first_generation / "competitor.txt").write_text("keep", encoding="utf-8")
            inserted = True

    monkeypatch.setattr(publisher, "_checkpoint", race)
    with pytest.raises(ForgeException) as error:
        publisher.publish(third)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert (first_generation / "competitor.txt").read_text(encoding="utf-8") == "keep"
    assert validate_skill_bundle(_active_files(tmp_path, third)) == third.manifest


def test_configuration_root_and_ancestor_are_opened_without_following_links(tmp_path: Path) -> None:
    rendered = _render()
    disabled = CanonicalSkillPublisher(SkillOutputConfig())
    with pytest.raises(ForgeException) as error:
        disabled.publish(rendered)
    _assert_error(error, ForgeErrorCode.OUTPUT_NOT_CONFIGURED)

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(ForgeException) as error:
        _publisher(linked).publish(rendered)
    _assert_error(error, ForgeErrorCode.PATH_NOT_ALLOWED)
    assert list(real.iterdir()) == []
