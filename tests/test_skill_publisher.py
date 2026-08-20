"""Filesystem, atomicity, and restart tests for canonical Agent Skill publication."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from test_skill_render import _analyzed, _render, _snapshot

from cove_book_forge.config import SkillOutputConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs import AgentSkillRenderer
from cove_book_forge.outputs import skill_publisher as skill_publisher_module
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


def _run_skill_cleanup_hard_crash(root: Path, checkpoint: str) -> None:
    script = r"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "tests"))
from test_skill_render import _analyzed, _render, _snapshot

from cove_book_forge.config import SkillOutputConfig
from cove_book_forge.outputs import AgentSkillRenderer
from cove_book_forge.outputs.skill_publisher import CanonicalSkillPublisher

root = Path(os.environ["COVE_SKILL_CRASH_ROOT"])
checkpoint = os.environ["COVE_SKILL_CRASH_POINT"]
first = _render()
renderer = AgentSkillRenderer(SkillOutputConfig())
second = renderer.render(
    _snapshot(chapter_index=1, title="Second chapter"),
    _analyzed(fingerprint="2" * 64),
    first.manifest,
)
third = renderer.render(
    _snapshot(chapter_index=2, title="Third chapter"),
    _analyzed(fingerprint="3" * 64),
    second.manifest,
)

def crash(_self, phase):
    if phase == checkpoint:
        os._exit(86)

CanonicalSkillPublisher._checkpoint = crash
CanonicalSkillPublisher(
    SkillOutputConfig(enabled=True, canonical_path=root)
).publish(third)
raise SystemExit(98)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "COVE_SKILL_CRASH_POINT": checkpoint,
            "COVE_SKILL_CRASH_ROOT": str(root),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 86, completed.stderr


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


@pytest.mark.parametrize("hardlink_target", ["management-owner", "generation-content"])
def test_owned_regular_files_with_multiple_links_fail_closed(
    tmp_path: Path, hardlink_target: str
) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    if hardlink_target == "management-owner":
        target = tmp_path / ".cove-book-forge" / ".owner.json"
    else:
        target = tmp_path / first.skill_slug / first.chapter_path
    source = tmp_path / f"{hardlink_target}-source"
    source.write_bytes(target.read_bytes())
    target.unlink()
    os.link(source, target)
    assert os.lstat(target).st_nlink == 2

    with pytest.raises(ForgeException) as error:
        publisher.publish(first if hardlink_target == "management-owner" else _next(first))

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)


def test_generation_tree_read_stops_at_the_aggregate_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cove_book_forge.outputs.skill_publisher as skill_publisher

    per_file = 8 * 1024 * 1024
    for index in range(9):
        path = tmp_path / f"payload-{index}.md"
        with path.open("wb") as stream:
            stream.truncate(per_file)
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    original_read = skill_publisher.os.read
    bytes_read = 0

    def counted_read(fd: int, count: int) -> bytes:
        nonlocal bytes_read
        payload = original_read(fd, count)
        bytes_read += len(payload)
        return payload

    monkeypatch.setattr(skill_publisher.os, "read", counted_read)
    try:
        with pytest.raises(ForgeException) as error:
            _publisher(tmp_path)._read_tree(descriptor)
    finally:
        os.close(descriptor)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert bytes_read <= 64 * 1024 * 1024 + 1


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


def _replace_directory_with_identity_distinct_copy(path: Path) -> None:
    detached = path.with_name(f"{path.name}-detached")
    path.rename(detached)
    shutil.copytree(detached, path, symlinks=True)


@pytest.mark.parametrize(
    ("relative_directory", "phase"),
    [
        (".cove-book-forge", "hierarchy:after-stage"),
        (".cove-book-forge/generations", "hierarchy:before-cas"),
        (".cove-book-forge/transactions", "hierarchy:after-cas"),
        (".cove-book-forge/activations", "hierarchy:before-return"),
    ],
)
def test_detached_management_hierarchy_can_never_report_publication_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_directory: str,
    phase: str,
) -> None:
    first = _render()
    second = _next(first)
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    replaced = False

    def replace(checkpoint: str) -> None:
        nonlocal replaced
        if checkpoint == phase and not replaced:
            _replace_directory_with_identity_distinct_copy(tmp_path / relative_directory)
            replaced = True

    monkeypatch.setattr(publisher, "_checkpoint", replace)
    with pytest.raises(ForgeException) as error:
        publisher.publish(second)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert replaced


@pytest.mark.parametrize("competitor_kind", ["file", "directory", "fifo", "symlink"])
def test_exchange_always_restores_a_displaced_nonmatching_competitor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, competitor_kind: str
) -> None:
    first = _render()
    second = _next(first)
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    active = tmp_path / first.skill_slug
    competitor_target = tmp_path / "competitor-target"
    competitor_target.mkdir()
    raced = False
    competitor_identity: tuple[int, int] | None = None

    def race(phase: str) -> None:
        nonlocal raced, competitor_identity
        if phase != "activation:exchange" or raced:
            return
        active.unlink()
        if competitor_kind == "file":
            active.write_bytes(b"competitor-file")
        elif competitor_kind == "directory":
            active.mkdir()
            (active / "identity.txt").write_text("competitor-directory", encoding="utf-8")
        elif competitor_kind == "fifo":
            os.mkfifo(active)
        else:
            active.symlink_to("competitor-target")
        status = os.lstat(active)
        competitor_identity = (status.st_dev, status.st_ino)
        raced = True

    monkeypatch.setattr(publisher, "_checkpoint", race)
    with pytest.raises(ForgeException) as error:
        publisher.publish(second)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert raced and competitor_identity is not None
    restored = os.lstat(active)
    assert (restored.st_dev, restored.st_ino) == competitor_identity
    if competitor_kind == "file":
        assert active.read_bytes() == b"competitor-file"
    elif competitor_kind == "directory":
        assert (active / "identity.txt").read_text(encoding="utf-8") == "competitor-directory"
    elif competitor_kind == "fifo":
        assert stat.S_ISFIFO(restored.st_mode)
    else:
        assert os.readlink(active) == "competitor-target"
    activation_pointers = [
        item
        for item in (tmp_path / ".cove-book-forge" / "activations").iterdir()
        if item.is_symlink()
    ]
    assert len(activation_pointers) == 1
    assert os.readlink(activation_pointers[0]).endswith(f"gen-{second.manifest.checksum}/content")


def test_exception_after_exchange_cannot_skip_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _render()
    second = _next(first)
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    old_target = os.readlink(tmp_path / first.skill_slug)

    def interrupt(phase: str) -> None:
        if phase == "activation:exchanged":
            raise SimulatedProcessCrash(phase)

    monkeypatch.setattr(publisher, "_checkpoint", interrupt)
    with pytest.raises(SimulatedProcessCrash, match="activation:exchanged"):
        publisher.publish(second)

    assert os.readlink(tmp_path / first.skill_slug) == old_target
    activation_pointers = [
        item
        for item in (tmp_path / ".cove-book-forge" / "activations").iterdir()
        if item.is_symlink()
    ]
    assert len(activation_pointers) == 1
    assert os.readlink(activation_pointers[0]).endswith(f"gen-{second.manifest.checksum}/content")


def test_signal_delivered_at_exchange_return_cannot_skip_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _render()
    second = _next(first)
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    old_target = os.readlink(tmp_path / first.skill_slug)
    rename_exchange = skill_publisher_module._rename_exchange
    interrupted = False

    def exchange_then_interrupt(
        source: str,
        destination: str,
        *,
        source_fd: int,
        destination_fd: int,
    ) -> None:
        nonlocal interrupted
        rename_exchange(
            source,
            destination,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )
        if not interrupted:
            interrupted = True
            raise SimulatedProcessCrash("signal at exchange return")

    monkeypatch.setattr(skill_publisher_module, "_rename_exchange", exchange_then_interrupt)
    with pytest.raises(SimulatedProcessCrash, match="exchange return"):
        publisher.publish(second)

    assert os.readlink(tmp_path / first.skill_slug) == old_target
    staged = [
        item
        for item in (tmp_path / ".cove-book-forge" / "activations").iterdir()
        if item.is_symlink()
    ]
    assert len(staged) == 1
    assert os.readlink(staged[0]).endswith(f"gen-{second.manifest.checksum}/content")


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


def test_transaction_recovery_preserves_a_same_size_wrong_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendered = _render()
    publisher = _publisher(tmp_path)

    def interrupt(phase: str) -> None:
        if phase == "stage:file":
            raise SimulatedProcessCrash(phase)

    monkeypatch.setattr(publisher, "_checkpoint", interrupt)
    with pytest.raises(SimulatedProcessCrash):
        publisher.publish(rendered)

    transactions = tmp_path / ".cove-book-forge" / "transactions"
    transaction = next(transactions.glob("tx-*"))
    content_file = next(path for path in (transaction / "content").rglob("*") if path.is_file())
    original = content_file.read_bytes()
    corrupted = bytes([original[0] ^ 0xFF]) + original[1:]
    content_file.write_bytes(corrupted)

    with pytest.raises(ForgeException) as error:
        _publisher(tmp_path).publish(rendered)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert transaction.is_dir()
    assert content_file.read_bytes() == corrupted


def test_transaction_final_delete_rechecks_the_owner_digest_after_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendered = _render()
    crashing = _publisher(tmp_path)

    def interrupt(phase: str) -> None:
        if phase == "stage:file":
            raise SimulatedProcessCrash(phase)

    monkeypatch.setattr(crashing, "_checkpoint", interrupt)
    with pytest.raises(SimulatedProcessCrash):
        crashing.publish(rendered)

    transaction = next((tmp_path / ".cove-book-forge" / "transactions").glob("tx-*"))
    content_file = next(path for path in (transaction / "content").rglob("*") if path.is_file())
    original = content_file.read_bytes()
    corrupted = bytes([original[0] ^ 0xFF]) + original[1:]
    publisher = _publisher(tmp_path)
    unlink_identity = publisher._unlink_identity
    raced = False

    def modify_before_isolation(
        directory_fd: int,
        name: str,
        identity: tuple[int, int],
        **kwargs: object,
    ) -> None:
        nonlocal raced
        if name == content_file.name and not raced:
            descriptor = os.open(name, os.O_WRONLY, dir_fd=directory_fd)
            try:
                os.write(descriptor, corrupted)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            raced = True
        unlink_identity(directory_fd, name, identity, **kwargs)

    monkeypatch.setattr(publisher, "_unlink_identity", modify_before_isolation)
    with pytest.raises(ForgeException) as error:
        publisher.publish(rendered)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert raced
    assert any(path.is_file() and path.read_bytes() == corrupted for path in transaction.rglob("*"))


@pytest.mark.parametrize("entry_kind", ["file", "directory"])
def test_final_cleanup_atomically_isolates_a_competitor_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    second = _next(first)
    publisher.publish(second)
    third = _next(second, chapter_index=2, title="Third chapter")
    rename_noreplace = skill_publisher_module._rename_noreplace
    competitor_identity: tuple[int, int] | None = None

    def race_entry_move(
        source: str,
        destination: str,
        *,
        source_fd: int,
        destination_fd: int,
    ) -> bool:
        nonlocal competitor_identity
        target = ".owner.json" if entry_kind == "file" else "chapters"
        if source == target and competitor_identity is None:
            os.rename(
                source,
                f"saved-{target}",
                src_dir_fd=source_fd,
                dst_dir_fd=source_fd,
            )
            if entry_kind == "file":
                descriptor = os.open(
                    source,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=source_fd,
                )
                try:
                    os.write(descriptor, b"competitor")
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            else:
                os.mkdir(source, 0o700, dir_fd=source_fd)
            status = os.stat(source, dir_fd=source_fd, follow_symlinks=False)
            competitor_identity = (status.st_dev, status.st_ino)
        return rename_noreplace(
            source,
            destination,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )

    monkeypatch.setattr(skill_publisher_module, "_rename_noreplace", race_entry_move)
    with pytest.raises(ForgeException) as error:
        publisher.publish(third)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert competitor_identity is not None
    quarantine = tmp_path / ".cove-book-forge" / "quarantine"
    identities: set[tuple[int, int]] = set()
    for path in quarantine.rglob("*"):
        status = os.lstat(path)
        identities.add((status.st_dev, status.st_ino))
    assert competitor_identity in identities
    assert validate_skill_bundle(_active_files(tmp_path, third)) == third.manifest


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


def test_never_activated_generation_never_displaces_the_actual_predecessor_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    first_target = os.readlink(tmp_path / first.skill_slug)
    never_activated = _next(first, chapter_index=2, title="Never activated")

    def interrupt(phase: str) -> None:
        if phase == "activation:before":
            raise SimulatedProcessCrash(phase)

    monkeypatch.setattr(publisher, "_checkpoint", interrupt)
    with pytest.raises(SimulatedProcessCrash):
        publisher.publish(never_activated)

    second = _next(first, chapter_index=1, title="Actually activated")
    _publisher(tmp_path).publish(second)

    generations = tmp_path / ".cove-book-forge" / "generations" / first.manifest.book_key
    names = {path.name for path in generations.iterdir()}
    assert Path(first_target).parts[-2] in names
    assert f"gen-{second.manifest.checksum}" in names
    assert f"gen-{never_activated.manifest.checksum}" not in names
    state = json.loads(
        (tmp_path / ".cove-book-forge" / "state" / f"{first.manifest.book_key}.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["current_target"].endswith(f"gen-{second.manifest.checksum}/content")
    assert state["previous_target"] == first_target


@pytest.mark.parametrize("has_previous", [False, True])
def test_marker_only_recovery_cleans_missing_staged_pointer_without_scan_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_previous: bool
) -> None:
    first = _render()
    if has_previous:
        _publisher(tmp_path).publish(first)
        rendered = _next(first)
    else:
        rendered = first
    publisher = _publisher(tmp_path)

    def interrupt(phase: str) -> None:
        if phase == "activation:before":
            raise SimulatedProcessCrash(phase)

    monkeypatch.setattr(publisher, "_checkpoint", interrupt)
    with pytest.raises(SimulatedProcessCrash):
        publisher.publish(rendered)
    activations = tmp_path / ".cove-book-forge" / "activations"
    for entry in activations.iterdir():
        if entry.is_symlink():
            entry.unlink()

    stable = first
    for _ in range(3):
        _publisher(tmp_path).publish(stable)

    assert list(activations.iterdir()) == []
    assert validate_skill_bundle(_active_files(tmp_path, stable)) == stable.manifest


@pytest.mark.parametrize("interruption", ["cleanup:quarantined", "cleanup:partial"])
def test_quarantined_generation_cleanup_is_restartable_after_process_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    second = _next(first)
    publisher.publish(second)
    third = _next(second, chapter_index=2, title="Third chapter")
    interrupted = False

    def interrupt(phase: str) -> None:
        nonlocal interrupted
        if phase == interruption and not interrupted:
            interrupted = True
            raise SimulatedProcessCrash(phase)

    monkeypatch.setattr(publisher, "_checkpoint", interrupt)
    with pytest.raises(SimulatedProcessCrash, match=interruption):
        publisher.publish(third)

    assert validate_skill_bundle(_active_files(tmp_path, third)) == third.manifest
    quarantine = tmp_path / ".cove-book-forge" / "quarantine"
    assert list(quarantine.iterdir())

    _publisher(tmp_path).publish(third)

    assert list(quarantine.iterdir()) == []
    generations = tmp_path / ".cove-book-forge" / "generations" / first.manifest.book_key
    assert {path.name for path in generations.iterdir()} == {
        f"gen-{second.manifest.checksum}",
        f"gen-{third.manifest.checksum}",
    }


def test_cleanup_quarantine_preserves_a_competitor_that_wins_the_source_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    first_generation = tmp_path / os.readlink(tmp_path / first.skill_slug)
    first_generation = first_generation.parent
    second = _next(first)
    publisher.publish(second)
    third = _next(second, chapter_index=2, title="Third chapter")
    raced = False

    def race(phase: str) -> None:
        nonlocal raced
        if phase == "cleanup:quarantine" and not raced:
            first_generation.rename(first_generation.with_name("saved-original"))
            first_generation.mkdir()
            (first_generation / "competitor.txt").write_text("keep", encoding="utf-8")
            raced = True

    monkeypatch.setattr(publisher, "_checkpoint", race)
    with pytest.raises(ForgeException) as error:
        publisher.publish(third)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert raced
    quarantine = tmp_path / ".cove-book-forge" / "quarantine"
    competitor_files = list(quarantine.rglob("competitor.txt"))
    assert len(competitor_files) == 1
    assert competitor_files[0].read_text(encoding="utf-8") == "keep"


def test_quarantine_recovery_rechecks_the_remaining_payload_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _render()
    publisher = _publisher(tmp_path)
    publisher.publish(first)
    second = _next(first)
    publisher.publish(second)
    third = _next(second, chapter_index=2, title="Third chapter")

    def interrupt(phase: str) -> None:
        if phase == "cleanup:quarantined":
            raise SimulatedProcessCrash(phase)

    monkeypatch.setattr(publisher, "_checkpoint", interrupt)
    with pytest.raises(SimulatedProcessCrash):
        publisher.publish(third)

    quarantine = tmp_path / ".cove-book-forge" / "quarantine"
    payload = next(quarantine.glob("q-*/payload/.owner.json"))
    original = payload.read_bytes()
    payload.write_bytes(b"X" * len(original))

    with pytest.raises(ForgeException) as error:
        _publisher(tmp_path).publish(third)

    _assert_error(error, ForgeErrorCode.EXTERNAL_MODIFICATION)
    assert payload.read_bytes() == b"X" * len(original)


def test_unpublished_wrapper_crashes_never_exhaust_the_quarantine_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _render()
    _publisher(tmp_path).publish(first)
    second = _next(first)
    _publisher(tmp_path).publish(second)
    third = _next(second, chapter_index=2, title="Third chapter")

    for _ in range(128):
        publisher = _publisher(tmp_path)

        def interrupt(phase: str) -> None:
            if phase == "cleanup:wrapper-staged":
                raise SimulatedProcessCrash(phase)

        monkeypatch.setattr(publisher, "_checkpoint", interrupt)
        with pytest.raises(SimulatedProcessCrash, match="wrapper-staged"):
            publisher.publish(third)

    _publisher(tmp_path).publish(third)

    quarantine = tmp_path / ".cove-book-forge" / "quarantine"
    assert list(quarantine.iterdir()) == []
    assert validate_skill_bundle(_active_files(tmp_path, third)) == third.manifest


def test_published_journal_only_wrapper_is_restartable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _render()
    _publisher(tmp_path).publish(first)
    second = _next(first)
    _publisher(tmp_path).publish(second)
    third = _next(second, chapter_index=2, title="Third chapter")
    publisher = _publisher(tmp_path)

    def interrupt(phase: str) -> None:
        if phase == "cleanup:wrapper-published":
            raise SimulatedProcessCrash(phase)

    monkeypatch.setattr(publisher, "_checkpoint", interrupt)
    with pytest.raises(SimulatedProcessCrash, match="wrapper-published"):
        publisher.publish(third)

    _publisher(tmp_path).publish(third)

    quarantine = tmp_path / ".cove-book-forge" / "quarantine"
    assert list(quarantine.iterdir()) == []
    assert validate_skill_bundle(_active_files(tmp_path, third)) == third.manifest


def test_terminal_journal_removal_hard_crash_recovers_the_proven_empty_wrapper(
    tmp_path: Path,
) -> None:
    first = _render()
    _publisher(tmp_path).publish(first)
    second = _next(first)
    _publisher(tmp_path).publish(second)
    third = _next(second, chapter_index=2, title="Third chapter")

    _run_skill_cleanup_hard_crash(tmp_path, "cleanup:terminal-journal-removed")
    assert validate_skill_bundle(_active_files(tmp_path, third)) == third.manifest

    _publisher(tmp_path).publish(third)

    quarantine = tmp_path / ".cove-book-forge" / "quarantine"
    assert list(quarantine.iterdir()) == []
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
