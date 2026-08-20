from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
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


def test_broad_root_policy_is_enforced_by_opened_directory_identity(monkeypatch) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    monkeypatch.setattr(publisher_module, "_is_broad", lambda _path: False)
    broad = Path.cwd()

    with pytest.raises(ForgeException) as raised:
        _publisher(broad).publish(lambda _previous: pytest.fail("renderer must not run"))

    assert raised.value.code is ForgeErrorCode.PATH_NOT_ALLOWED


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


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "card"),
    [
        ("_MAX_CANDIDATE_COUNT", 2, False),
        ("_MAX_TOTAL_TRANSACTION_BYTES", 1, False),
        ("_MAX_MANAGED_CHAPTERS", 0, False),
        ("_MAX_MANAGED_CARDS", 0, True),
    ],
)
def test_publication_budgets_fail_closed_before_filesystem_mutation(
    tmp_path: Path,
    monkeypatch,
    limit_name: str,
    limit_value: int,
    card: bool,
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setattr(publisher_module, limit_name, limit_value, raising=False)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(card=card))

    assert raised.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert list(vault.iterdir()) == []
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


def test_transaction_open_failure_removes_the_owned_empty_transaction_directory(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    real_open = publisher_module.os.open

    def fail_transaction_open(path, flags, mode=0o777, *, dir_fd=None):
        if isinstance(path, str) and path.startswith("tx-"):
            raise OSError("PRIVATE-TX-OPEN")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(publisher_module.os, "open", fail_transaction_open)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed())

    assert raised.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert list(vault.iterdir()) == []
    _assert_safe(raised.value, vault)


@pytest.mark.parametrize(
    ("phase", "signal"),
    [
        ("setup", KeyboardInterrupt("setup signal")),
        ("stage", SystemExit("stage signal")),
        ("commit", asyncio.CancelledError("commit cancellation")),
    ],
)
def test_non_exception_signals_are_cleaned_up_and_re_raised_exactly(
    tmp_path: Path,
    monkeypatch,
    phase: str,
    signal: BaseException,
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    if phase == "setup":
        real_mkdir = publisher_module.os.mkdir

        def interrupt_setup(path, *args, **kwargs):
            if path == "stage":
                raise signal
            return real_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(publisher_module.os, "mkdir", interrupt_setup)
    elif phase == "stage":
        monkeypatch.setattr(
            publisher_module.os, "write", lambda *_args, **_kwargs: (_ for _ in ()).throw(signal)
        )
    else:
        monkeypatch.setattr(
            publisher_module.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(signal)
        )

    with pytest.raises(type(signal)) as raised:
        _publish(vault, _analyzed())

    assert raised.value is signal
    assert list(vault.iterdir()) == []


@pytest.mark.parametrize("rollback_hook", ["remove", "restore"])
def test_rollback_signal_finishes_recovery_before_exact_re_raise(
    tmp_path: Path, monkeypatch, rollback_hook: str
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    before = _visible(vault)
    signal = KeyboardInterrupt(f"rollback {rollback_hook} signal")
    real_link = publisher_module.os.link
    manifest_failed = False

    def fail_manifest_link(src, dst, **kwargs):
        nonlocal manifest_failed
        if not manifest_failed and isinstance(dst, str) and dst.endswith(".json"):
            manifest_failed = True
            raise OSError("PRIVATE-MANIFEST-FAILURE")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(publisher_module.os, "link", fail_manifest_link)
    method_name = "_remove_published" if rollback_hook == "remove" else "_restore_backup"
    original = getattr(publisher_module.GuardedPublisher, method_name)
    fired = False

    def interrupt_after_recovery(self, *args, **kwargs):
        nonlocal fired
        result = original(self, *args, **kwargs)
        if not fired:
            fired = True
            raise signal
        return result

    monkeypatch.setattr(publisher_module.GuardedPublisher, method_name, interrupt_after_recovery)

    with pytest.raises(KeyboardInterrupt) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value is signal
    assert fired
    assert _visible(vault) == before
    _assert_no_transactions(vault)


def test_directory_child_fd_closes_when_post_open_validation_fails(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "existing").mkdir()
    anchor = publisher_module._VaultAnchor.capture(
        ObsidianOutputConfig(enabled=True, vault_path=vault)
    )
    real_open = publisher_module.os.open
    real_stat = publisher_module.os.stat
    opened_child: list[int] = []

    def record_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "existing":
            opened_child.append(descriptor)
        return descriptor

    def fail_post_open_stat(path, *, dir_fd=None, follow_symlinks=True):
        if path == "existing":
            raise OSError("PRIVATE-POST-OPEN")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(publisher_module.os, "open", record_open)
    monkeypatch.setattr(publisher_module.os, "stat", fail_post_open_stat)
    try:
        with pytest.raises((ForgeException, OSError)):
            _publisher(vault)._open_directory(anchor, "existing", create=False)
    finally:
        anchor.close()

    assert len(opened_child) == 1
    with pytest.raises(OSError):
        os.fstat(opened_child[0])


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


def test_stage_inode_is_revalidated_immediately_before_each_publish(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    before = _visible(vault)
    real_publish = publisher_module.GuardedPublisher._publish_stage
    fired = False

    def mutate_stage_then_publish(self, anchor, transaction, path, expected):
        nonlocal fired
        if not fired and path == first.rendered.chapter_path:
            fired = True
            staged = transaction.staged[path]
            read_fd = os.open(staged.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=transaction.stage_fd)
            try:
                payload = os.read(read_fd, 32 * 1024 * 1024)
            finally:
                os.close(read_fd)
            write_fd = os.open(
                staged.name, os.O_WRONLY | os.O_NOFOLLOW, dir_fd=transaction.stage_fd
            )
            try:
                os.write(write_fd, bytes([payload[0] ^ 1]) + payload[1:])
                os.fsync(write_fd)
            finally:
                os.close(write_fd)
        return real_publish(self, anchor, transaction, path, expected)

    monkeypatch.setattr(
        publisher_module.GuardedPublisher, "_publish_stage", mutate_stage_then_publish
    )

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert _visible(vault) == before


def test_manifest_linearization_revalidates_every_published_non_manifest_file(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    chapter = vault / first.rendered.chapter_path
    old_chapter = chapter.read_bytes()
    competitor = b"competitor after chapter publication"
    real_publish = publisher_module.GuardedPublisher._publish_stage
    fired = False

    def replace_published_chapter(self, anchor, transaction, path, expected):
        nonlocal fired
        result = real_publish(self, anchor, transaction, path, expected)
        if not fired and path == first.rendered.chapter_path:
            fired = True
            chapter.unlink()
            chapter.write_bytes(competitor)
        return result

    monkeypatch.setattr(
        publisher_module.GuardedPublisher, "_publish_stage", replace_published_chapter
    )

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert chapter.read_bytes() == competitor
    backups = (vault / ".cove-book-forge" / ".transactions").rglob("backup/*")
    assert any(path.read_bytes() == old_chapter for path in backups)


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo"])
def test_rollback_restores_unexpected_object_without_following_or_deleting_it(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    chapter = vault / first.rendered.chapter_path
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    real_publish = publisher_module.GuardedPublisher._publish_stage
    fired = False

    def replace_after_publish(self, anchor, transaction, path, expected):
        nonlocal fired
        result = real_publish(self, anchor, transaction, path, expected)
        if not fired and path == first.rendered.chapter_path:
            fired = True
            chapter.unlink()
            if kind == "directory":
                chapter.mkdir()
                (chapter / "marker").write_bytes(b"rollback directory")
            elif kind == "symlink":
                chapter.symlink_to(outside)
            else:
                os.mkfifo(chapter)
        return result

    monkeypatch.setattr(publisher_module.GuardedPublisher, "_publish_stage", replace_after_publish)

    with pytest.raises(ForgeException):
        _publish(vault, _analyzed(fingerprint="b" * 64))

    if kind == "directory":
        assert (chapter / "marker").read_bytes() == b"rollback directory"
    elif kind == "symlink":
        assert chapter.is_symlink() and chapter.readlink() == outside
    else:
        assert stat.S_ISFIFO(chapter.stat(follow_symlinks=False).st_mode)


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


def test_backup_rename_success_followed_by_an_error_does_not_lose_the_old_file(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    before = _visible(vault)
    chapter = vault / first.rendered.chapter_path
    real_rename = publisher_module.os.rename
    fired = False

    def rename_then_fail(src, dst, **kwargs):
        nonlocal fired
        result = real_rename(src, dst, **kwargs)
        if not fired and src == chapter.name:
            fired = True
            raise OSError("PRIVATE-POST-BACKUP-RENAME")
        return result

    monkeypatch.setattr(publisher_module.os, "rename", rename_then_fail)

    with pytest.raises(ForgeException):
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert fired
    assert _visible(vault) == before
    _assert_no_transactions(vault)


def test_rollback_rename_success_followed_by_an_error_still_restores_old_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    before = _visible(vault)
    real_link = publisher_module.os.link
    real_rename = publisher_module.os.rename
    manifest_failed = False
    rollback_failed = False

    def fail_manifest_once(src, dst, **kwargs):
        nonlocal manifest_failed
        if not manifest_failed and isinstance(dst, str) and dst.endswith(".json"):
            manifest_failed = True
            raise OSError("PRIVATE-MANIFEST-FAILURE")
        return real_link(src, dst, **kwargs)

    def rollback_rename_then_fail(src, dst, **kwargs):
        nonlocal rollback_failed
        result = real_rename(src, dst, **kwargs)
        if not rollback_failed and isinstance(dst, str) and dst.startswith("r"):
            rollback_failed = True
            raise OSError("PRIVATE-POST-ROLLBACK-RENAME")
        return result

    monkeypatch.setattr(publisher_module.os, "link", fail_manifest_once)
    monkeypatch.setattr(publisher_module.os, "rename", rollback_rename_then_fail)

    with pytest.raises(ForgeException):
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert manifest_failed and rollback_failed
    assert _visible(vault) == before
    _assert_no_transactions(vault)


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo"])
def test_unexpected_object_moved_during_backup_is_restored_without_following_it(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    chapter = vault / first.rendered.chapter_path
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside remains untouched")
    real_rename = publisher_module.os.rename
    fired = False

    def replace_then_rename(src, dst, **kwargs):
        nonlocal fired
        if not fired and src == chapter.name:
            fired = True
            chapter.unlink()
            if kind == "directory":
                chapter.mkdir()
                (chapter / "marker").write_bytes(b"directory competitor")
            elif kind == "symlink":
                chapter.symlink_to(outside)
            else:
                os.mkfifo(chapter)
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr(publisher_module.os, "rename", replace_then_rename)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    if kind == "directory":
        assert chapter.is_dir()
        assert (chapter / "marker").read_bytes() == b"directory competitor"
    elif kind == "symlink":
        assert chapter.is_symlink()
        assert chapter.readlink() == outside
        assert outside.read_bytes() == b"outside remains untouched"
    else:
        assert stat.S_ISFIFO(chapter.stat(follow_symlinks=False).st_mode)


def test_transaction_fifo_read_is_nonblocking_and_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "transaction"
    directory.mkdir()
    os.mkfifo(directory / "candidate")
    script = """
import os
from pathlib import Path
from cove_book_forge.config import ObsidianOutputConfig
from cove_book_forge.errors import ForgeException
from cove_book_forge.outputs.publisher import GuardedPublisher

root = Path(os.environ["COVE_FIFO_TEST_DIR"])
directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    try:
        GuardedPublisher(ObsidianOutputConfig())._snapshot_in_directory(
            directory_fd, "candidate"
        )
    except ForgeException:
        raise SystemExit(0)
    raise SystemExit(3)
finally:
    os.close(directory_fd)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "COVE_FIFO_TEST_DIR": str(directory)},
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


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
    recovery_records = list((vault / ".cove-book-forge" / ".transactions").rglob("recovery-*.json"))
    assert len(recovery_records) == 1
    recovery = json.loads(recovery_records[0].read_bytes())
    assert recovery == {
        "container": "backup",
        "moved_name": recovery["moved_name"],
        "original_path": first.rendered.chapter_path,
        "recovery_id": recovery_records[0].stem.removeprefix("recovery-"),
        "schema": 1,
    }
    assert recovery["moved_name"].startswith("b")
    assert (recovery_records[0].parent / "backup" / recovery["moved_name"]).is_file()
    assert str(vault) not in recovery_records[0].read_text()
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
