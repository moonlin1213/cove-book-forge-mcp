from __future__ import annotations

import json
import os
import traceback
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
from cove_book_forge.outputs.obsidian_render import ObsidianRenderer
from cove_book_forge.outputs.publisher import GuardedPublisher


def _snapshot() -> ChapterSnapshot:
    return ChapterSnapshot(
        source_system="publisher-tests",
        external_book_id="book-atomic",
        book=BookMetadata(title="Atomic book", total_chapters=1),
        chapter=ChapterContent(index=0, title="Transaction", content="Private source."),
    )


def _analyzed(*, fingerprint: str = "a" * 64, card: bool = False) -> AnalyzedChapter:
    return AnalyzedChapter(
        input_fingerprint=fingerprint,
        cache_hit=True,
        analysis=ChapterAnalysis(
            core_idea=f"Bundle {fingerprint[0]}",
            concepts=(Concept(term="Atomicity", definition="All or nothing."),) if card else (),
        ),
    )


def _publisher(vault: Path) -> GuardedPublisher:
    return GuardedPublisher(ObsidianOutputConfig(enabled=True, vault_path=vault))


def _publish(vault: Path, analyzed: AnalyzedChapter):
    renderer = ObsidianRenderer(ObsidianOutputConfig(enabled=True, vault_path=vault))
    return _publisher(vault).publish(
        lambda previous: renderer.render(_snapshot(), analyzed, previous)
    )


def _visible(vault: Path) -> dict[str, bytes]:
    return {
        path.relative_to(vault).as_posix(): path.read_bytes()
        for path in vault.rglob("*")
        if path.is_file() and "/.transactions/" not in path.as_posix()
    }


def _assert_no_transactions(vault: Path) -> None:
    transactions = vault / ".cove-book-forge" / ".transactions"
    assert not transactions.exists() or not any(transactions.iterdir())


def _assert_safe(error: ForgeException, vault: Path) -> None:
    public = repr(error) + str(error) + json.dumps(error.as_result(), ensure_ascii=False)
    assert str(vault) not in public
    assert "Private source" not in public
    assert error.__cause__ is None


@pytest.mark.parametrize("broad", [Path("/"), Path.home(), Path.cwd()])
def test_broad_vault_roots_are_rejected_without_writes(broad: Path) -> None:
    with pytest.raises(ForgeException) as raised:
        _publisher(broad).publish(lambda _previous: pytest.fail("renderer must not run"))

    assert raised.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    _assert_safe(raised.value, broad)


def test_unwritable_vault_is_a_fixed_permission_error(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o500)
    try:
        with pytest.raises(ForgeException) as raised:
            _publish(vault, _analyzed())
    finally:
        vault.chmod(0o700)

    assert raised.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    _assert_safe(raised.value, vault)


def test_transaction_setup_failure_removes_only_owned_empty_directories(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    real_mkdir = publisher_module.os.mkdir

    def fail_stage_directory(path, *args, **kwargs):
        if path == "stage":
            raise OSError("PRIVATE-SETUP")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(publisher_module.os, "mkdir", fail_stage_directory)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed())

    assert raised.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert list(vault.iterdir()) == []
    _assert_safe(raised.value, vault)


def test_staged_bytes_are_verified_before_any_visible_mutation(tmp_path: Path, monkeypatch) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    before = _visible(vault)
    real_write = publisher_module.os.write
    fired = False

    def corrupt_write(descriptor: int, data: bytes) -> int:
        nonlocal fired
        if not fired and data:
            fired = True
            changed = bytes([data[0] ^ 1]) + data[1:]
            return real_write(descriptor, changed)
        return real_write(descriptor, data)

    monkeypatch.setattr(publisher_module.os, "write", corrupt_write)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert _visible(vault) == before
    assert (vault / first.rendered.chapter_path).is_file()


@pytest.mark.parametrize("unsafe", ["parent_symlink", "target_symlink", "target_directory"])
def test_symlink_or_non_file_components_are_never_followed(tmp_path: Path, unsafe: str) -> None:
    vault = tmp_path / "vault"
    outside = tmp_path / "outside"
    vault.mkdir()
    outside.mkdir()
    first_render = ObsidianRenderer(ObsidianOutputConfig(enabled=True, vault_path=vault)).render(
        _snapshot(), _analyzed(), None
    )
    target = vault / first_render.chapter_path
    if unsafe == "parent_symlink":
        (vault / "Books").symlink_to(outside, target_is_directory=True)
    else:
        target.parent.mkdir(parents=True)
        if unsafe == "target_symlink":
            private = outside / "private.md"
            private.write_bytes(b"competitor")
            target.symlink_to(private)
        else:
            target.mkdir()

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed())

    assert raised.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert not (outside / "Books").exists()
    _assert_safe(raised.value, vault)


def test_first_publish_never_overwrites_an_unmanaged_regular_target(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    rendered = ObsidianRenderer(ObsidianOutputConfig(enabled=True, vault_path=vault)).render(
        _snapshot(), _analyzed(), None
    )
    target = vault / rendered.chapter_path
    target.parent.mkdir(parents=True)
    target.write_bytes(b"private unmanaged note")

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed())

    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert target.read_bytes() == b"private unmanaged note"
    _assert_safe(raised.value, vault)


@pytest.mark.parametrize("failure", ["stage", "backup", "publish", "middle", "manifest", "cleanup"])
def test_injected_failures_restore_the_last_visible_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed(card=True))
    before = _visible(vault)
    manifest_name = next(path for path in before if path.endswith(".json"))

    if failure == "stage":
        real_write = publisher_module.os.write
        fired = False

        def fail_write(fd: int, data: bytes) -> int:
            nonlocal fired
            if not fired:
                fired = True
                raise OSError("PRIVATE-STAGE")
            return real_write(fd, data)

        monkeypatch.setattr(publisher_module.os, "write", fail_write)
    elif failure == "backup":
        real_rename = publisher_module.os.rename
        fired = False

        def fail_rename(src, dst, **kwargs):
            nonlocal fired
            if not fired and isinstance(src, str) and not src.startswith("."):
                fired = True
                raise OSError("PRIVATE-BACKUP")
            return real_rename(src, dst, **kwargs)

        monkeypatch.setattr(publisher_module.os, "rename", fail_rename)
    elif failure in {"publish", "middle", "manifest"}:
        real_link = publisher_module.os.link
        fired = False
        non_manifest_links = 0

        def fail_link(src, dst, **kwargs):
            nonlocal fired, non_manifest_links
            is_manifest = isinstance(dst, str) and dst == Path(manifest_name).name
            if not is_manifest:
                non_manifest_links += 1
            selected = (
                is_manifest
                if failure == "manifest"
                else not is_manifest and (failure == "publish" or non_manifest_links == 2)
            )
            if not fired and selected:
                fired = True
                raise OSError("PRIVATE-PUBLISH")
            return real_link(src, dst, **kwargs)

        monkeypatch.setattr(publisher_module.os, "link", fail_link)
    else:
        original = publisher_module.GuardedPublisher._cleanup_committed

        def fail_cleanup(self, transaction):
            raise OSError("PRIVATE-CLEANUP")

        monkeypatch.setattr(publisher_module.GuardedPublisher, "_cleanup_committed", fail_cleanup)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value.code in {
        ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
        ForgeErrorCode.EXTERNAL_MODIFICATION,
    }, "".join(traceback.format_tb(raised.tb))
    assert _visible(vault) == before
    _assert_no_transactions(vault)
    _assert_safe(raised.value, vault)
    if failure == "cleanup":
        monkeypatch.setattr(publisher_module.GuardedPublisher, "_cleanup_committed", original)


def test_competitor_replacement_before_backup_is_preserved(tmp_path: Path, monkeypatch) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    chapter = vault / first.rendered.chapter_path
    competitor = b"competitor replacement"
    real_rename = publisher_module.os.rename
    fired = False

    def race_rename(src, dst, **kwargs):
        nonlocal fired
        if not fired and src == chapter.name:
            fired = True
            chapter.unlink()
            chapter.write_bytes(competitor)
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr(publisher_module.os, "rename", race_rename)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert chapter.read_bytes() == competitor
    _assert_safe(raised.value, vault)


def test_competitor_creation_before_publish_is_never_overwritten_or_deleted(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    chapter = vault / first.rendered.chapter_path
    competitor = b"competitor won the absent-target race"
    real_link = publisher_module.os.link
    fired = False

    def race_link(src, dst, **kwargs):
        nonlocal fired
        destination_fd = kwargs.get("dst_dir_fd")
        if not fired and dst == chapter.name and destination_fd is not None:
            fired = True
            descriptor = os.open(
                dst,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=destination_fd,
            )
            try:
                os.write(descriptor, competitor)
            finally:
                os.close(descriptor)
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(publisher_module.os, "link", race_link)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert chapter.read_bytes() == competitor
    backups = list((vault / ".cove-book-forge" / ".transactions").rglob("backup/*"))
    assert any(path.read_bytes() != competitor for path in backups)
    _assert_safe(raised.value, vault)


def test_idempotent_publish_performs_no_mutating_filesystem_calls(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())

    def unexpected(*_args, **_kwargs):
        pytest.fail("idempotent publish attempted a filesystem mutation")

    for name in ("mkdir", "rename", "link", "unlink", "rmdir", "write"):
        monkeypatch.setattr(publisher_module.os, name, unexpected)

    receipt = _publish(vault, _analyzed())
    assert receipt.unchanged is True


def test_target_parent_replacement_before_publish_is_not_entered(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    chapter = vault / first.rendered.chapter_path
    old_chapter = chapter.read_bytes()
    original_parent = chapter.parent
    displaced_parent = original_parent.with_name("displaced-chapters")
    real_publish = publisher_module.GuardedPublisher._publish_stage
    fired = False

    def replace_parent(self, anchor, transaction, path, expected):
        nonlocal fired
        if not fired and path == first.rendered.chapter_path:
            fired = True
            original_parent.rename(displaced_parent)
            original_parent.mkdir()
            (original_parent / "competitor.txt").write_bytes(b"untouched")
        return real_publish(self, anchor, transaction, path, expected)

    monkeypatch.setattr(publisher_module.GuardedPublisher, "_publish_stage", replace_parent)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert {path.name for path in original_parent.iterdir()} == {"competitor.txt"}
    assert (original_parent / "competitor.txt").read_bytes() == b"untouched"
    assert any(
        path.read_bytes() == old_chapter
        for path in (vault / ".cove-book-forge" / ".transactions").rglob("backup/*")
    )


@pytest.mark.parametrize("verify_call", [2, 4, 8])
def test_root_replacement_during_transaction_is_detected(
    tmp_path: Path, monkeypatch, verify_call: int
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    displaced = tmp_path / "displaced"
    vault.mkdir()
    calls = 0
    real_verify = publisher_module._VaultAnchor.verify

    def replace_on_later_verify(self):
        nonlocal calls
        calls += 1
        if calls == verify_call:
            vault.rename(displaced)
            vault.mkdir()
            (vault / "competitor.txt").write_bytes(b"untouched")
        return real_verify(self)

    monkeypatch.setattr(publisher_module._VaultAnchor, "verify", replace_on_later_verify)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed())

    assert raised.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert (vault / "competitor.txt").read_bytes() == b"untouched"
    _assert_safe(raised.value, vault)
