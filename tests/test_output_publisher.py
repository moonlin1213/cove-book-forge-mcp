from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
import subprocess
import sys
import traceback
import weakref
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


def test_initial_render_is_released_before_manifest_read_and_second_render(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    renderer = ObsidianRenderer(ObsidianOutputConfig(enabled=True, vault_path=vault))
    initial_reference: weakref.ReferenceType[object] | None = None
    calls = 0

    def render(previous):
        nonlocal calls, initial_reference
        calls += 1
        if calls == 1:
            initial = renderer.render(_snapshot(), _analyzed(), previous)
            initial_reference = weakref.ref(initial)
            return initial
        assert initial_reference is not None
        assert initial_reference() is None
        return renderer.render(_snapshot(), _analyzed(), previous)

    receipt = _publisher(vault).publish(render)

    assert calls == 2
    assert receipt.unchanged is False


def test_initial_render_budget_failure_does_not_invoke_second_render(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    renderer = ObsidianRenderer(ObsidianOutputConfig(enabled=True, vault_path=vault))
    calls = 0

    def render(previous):
        nonlocal calls
        calls += 1
        return renderer.render(_snapshot(), _analyzed(), previous)

    monkeypatch.setattr(publisher_module, "_MAX_TOTAL_TRANSACTION_BYTES", 1)

    with pytest.raises(ForgeException) as raised:
        _publisher(vault).publish(render)

    assert raised.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert calls == 1
    assert list(vault.iterdir()) == []


def test_second_pass_growth_is_rejected_from_stat_before_reading_beyond_budget(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    chapter = vault / first.rendered.chapter_path
    renderer = ObsidianRenderer(ObsidianOutputConfig(enabled=True, vault_path=vault))
    updated = renderer.render(
        _snapshot(),
        _analyzed(fingerprint="b" * 64),
        first.rendered.manifest,
    )
    existing_size = sum(len(data) for data in _visible(vault).values())
    staged_size = sum(len(data) for data in updated.files.values())
    monkeypatch.setattr(
        publisher_module,
        "_MAX_TOTAL_TRANSACTION_BYTES",
        existing_size + staged_size + 8,
    )
    real_stage = publisher_module.GuardedPublisher._stage
    real_read = publisher_module.os.read
    growth_started = False
    grew_inode: tuple[int, int] | None = None
    read_grown_file = False

    def stage_then_grow(self, transaction, writes):
        nonlocal growth_started, grew_inode
        result = real_stage(self, transaction, writes)
        with chapter.open("ab") as stream:
            stream.write(b"x" * 64)
            stream.flush()
            os.fsync(stream.fileno())
        status = chapter.stat()
        grew_inode = (status.st_dev, status.st_ino)
        growth_started = True
        return result

    def record_read(descriptor: int, size: int) -> bytes:
        nonlocal read_grown_file
        status = os.fstat(descriptor)
        if growth_started and grew_inode == (status.st_dev, status.st_ino):
            read_grown_file = True
        return real_read(descriptor, size)

    monkeypatch.setattr(publisher_module.GuardedPublisher, "_stage", stage_then_grow)
    monkeypatch.setattr(publisher_module.os, "read", record_read)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
    assert growth_started
    assert read_grown_file is False


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


def test_original_keyboard_interrupt_precedes_rollback_record_error_and_converges(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    before = _visible(vault)
    real_link = publisher_module.os.link
    real_protect = publisher_module.GuardedPublisher._protect_moved
    original_signal = KeyboardInterrupt("original publish interrupt")
    link_fired = False
    rollback_fired = False

    def link_then_interrupt(src, dst, **kwargs):
        nonlocal link_fired
        result = real_link(src, dst, **kwargs)
        if not link_fired and dst == Path(first.rendered.chapter_path).name:
            link_fired = True
            raise original_signal
        return result

    def fail_first_rollback_intent(self, transaction, directory_fd, container, moved_name, path):
        nonlocal rollback_fired
        if not rollback_fired and container == "transaction":
            rollback_fired = True
            raise OSError("PRIVATE-ROLLBACK-INTENT")
        return real_protect(self, transaction, directory_fd, container, moved_name, path)

    monkeypatch.setattr(publisher_module.os, "link", link_then_interrupt)
    monkeypatch.setattr(
        publisher_module.GuardedPublisher,
        "_protect_moved",
        fail_first_rollback_intent,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value is original_signal
    assert link_fired and rollback_fired
    assert _visible(vault) == before
    _assert_no_transactions(vault)


def test_signal_after_forget_transition_is_preserved_after_rollback_convergence(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    before = _visible(vault)
    real_link = publisher_module.os.link
    real_forget = publisher_module.GuardedPublisher._forget_moved
    manifest_failed = False
    signal = SystemExit("post-forget transition")
    fired = False

    def fail_manifest_once(src, dst, **kwargs):
        nonlocal manifest_failed
        if not manifest_failed and isinstance(dst, str) and dst.endswith(".json"):
            manifest_failed = True
            raise OSError("PRIVATE-MANIFEST")
        return real_link(src, dst, **kwargs)

    def forget_then_interrupt(self, transaction, directory_fd, moved_name):
        nonlocal fired
        result = real_forget(self, transaction, directory_fd, moved_name)
        if not fired and moved_name.startswith("r"):
            fired = True
            raise signal
        return result

    monkeypatch.setattr(publisher_module.os, "link", fail_manifest_once)
    monkeypatch.setattr(
        publisher_module.GuardedPublisher,
        "_forget_moved",
        forget_then_interrupt,
    )

    with pytest.raises(SystemExit) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value is signal
    assert manifest_failed and fired
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


@pytest.mark.parametrize(
    "signal",
    [KeyboardInterrupt("absolute open interrupt"), asyncio.CancelledError("absolute open cancel")],
)
def test_absolute_directory_open_closes_every_fd_on_fstat_signal(
    tmp_path: Path, monkeypatch, signal: BaseException
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    real_open = publisher_module.os.open
    real_fstat = publisher_module.os.fstat
    opened: list[int] = []
    fired = False

    def record_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    def interrupt_final_fstat(descriptor: int):
        nonlocal fired
        if opened and descriptor == opened[-1] and not fired:
            fired = True
            raise signal
        return real_fstat(descriptor)

    monkeypatch.setattr(publisher_module.os, "open", record_open)
    monkeypatch.setattr(publisher_module.os, "fstat", interrupt_final_fstat)

    with pytest.raises(type(signal)) as raised:
        publisher_module._open_absolute_directory(vault, missing_is_unconfigured=False)

    assert raised.value is signal
    assert fired
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            real_fstat(descriptor)


@pytest.mark.parametrize("hook", ["_backup", "_remove_published", "_restore_backup"])
@pytest.mark.parametrize(
    "signal",
    [KeyboardInterrupt("transition interrupt"), asyncio.CancelledError("transition cancel")],
)
def test_transaction_parent_fd_closes_on_fstat_signal(
    tmp_path: Path,
    monkeypatch,
    hook: str,
    signal: BaseException,
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    before = _visible(vault)
    original_hook = getattr(publisher_module.GuardedPublisher, hook)
    real_open_directory = publisher_module.GuardedPublisher._open_directory
    real_fstat = publisher_module.os.fstat
    real_link = publisher_module.os.link
    armed = False
    fired = False
    matching_opens = 0
    acquired: list[int] = []
    manifest_failed = False

    def arm_hook(self, *args, **kwargs):
        nonlocal armed
        armed = True
        try:
            return original_hook(self, *args, **kwargs)
        finally:
            armed = False

    def record_open_directory(self, anchor, relative, **kwargs):
        nonlocal matching_opens
        descriptor = real_open_directory(self, anchor, relative, **kwargs)
        if armed and relative.endswith("Chapters") and descriptor is not None:
            matching_opens += 1
            wanted = matching_opens == (2 if hook == "_backup" else 1)
            if wanted:
                acquired.append(descriptor)
        return descriptor

    def interrupt_owned_fstat(descriptor: int):
        nonlocal fired
        if armed and acquired and descriptor == acquired[-1] and not fired:
            fired = True
            raise signal
        return real_fstat(descriptor)

    def fail_manifest_once(src, dst, **kwargs):
        nonlocal manifest_failed
        if (
            hook != "_backup"
            and not manifest_failed
            and isinstance(dst, str)
            and dst.endswith(".json")
        ):
            manifest_failed = True
            raise OSError("PRIVATE-MANIFEST-FAILURE")
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(publisher_module.GuardedPublisher, hook, arm_hook)
    monkeypatch.setattr(
        publisher_module.GuardedPublisher,
        "_open_directory",
        record_open_directory,
    )
    monkeypatch.setattr(publisher_module.os, "fstat", interrupt_owned_fstat)
    monkeypatch.setattr(publisher_module.os, "link", fail_manifest_once)

    with pytest.raises(type(signal)) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value is signal
    assert fired
    assert acquired
    for descriptor in acquired:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert _visible(vault) == before


def test_fsync_never_treats_ebadf_as_an_unsupported_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    def fail_fsync(_descriptor: int) -> None:
        raise OSError(errno.EBADF, "bad descriptor")

    monkeypatch.setattr(publisher_module.os, "fsync", fail_fsync)

    with pytest.raises(OSError) as file_error:
        publisher_module._fsync_file(100)
    with pytest.raises(OSError) as directory_error:
        publisher_module._fsync_directory(101)

    assert file_error.value.errno == errno.EBADF
    assert directory_error.value.errno == errno.EBADF


@pytest.mark.parametrize("unsupported", [errno.EINVAL, errno.ENOTSUP])
def test_only_explicit_directory_fsync_unsupported_errors_are_tolerated(
    monkeypatch: pytest.MonkeyPatch, unsupported: int
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    monkeypatch.setattr(
        publisher_module.os,
        "fsync",
        lambda _descriptor: (_ for _ in ()).throw(OSError(unsupported, "unsupported")),
    )

    publisher_module._fsync_directory(100)
    with pytest.raises(OSError) as raised:
        publisher_module._fsync_file(101)
    assert raised.value.errno == unsupported


@pytest.mark.parametrize(
    "phase",
    [
        "stage",
        "recovery",
        "transaction_state_file",
        "transaction_state_directory",
        "backup_parent",
        "target_parent",
        "manifest",
    ],
)
def test_ebadf_durability_failure_never_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed(card=True))
    before = _visible(vault)
    context: list[str] = []
    fired = False
    real_file_fsync = publisher_module._fsync_file
    real_directory_fsync = publisher_module._fsync_directory

    def wrap(name: str):
        original = getattr(publisher_module.GuardedPublisher, name)

        def wrapped(self, *args, **kwargs):
            if name == "_publish_stage":
                label = "manifest" if args[2].endswith(".json") else "target_parent"
            elif name == "_protect_moved":
                label = "recovery"
            elif name == "_backup":
                label = "backup_parent"
            elif name == "_persist_transaction_phase":
                label = "transaction_state"
            else:
                label = "stage"
            context.append(label)
            try:
                return original(self, *args, **kwargs)
            finally:
                context.pop()

        monkeypatch.setattr(publisher_module.GuardedPublisher, name, wrapped)

    for method in (
        "_stage",
        "_protect_moved",
        "_persist_transaction_phase",
        "_backup",
        "_publish_stage",
    ):
        wrap(method)

    def fail_file_fsync(descriptor: int) -> None:
        nonlocal fired
        selected = context and (
            context[-1] == phase
            or (context[-1] == "transaction_state" and phase == "transaction_state_file")
        )
        if not fired and selected:
            fired = True
            raise OSError(errno.EBADF, "bad descriptor")
        real_file_fsync(descriptor)

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal fired
        selected = context and (
            context[-1] == phase
            or (context[-1] == "transaction_state" and phase == "transaction_state_directory")
        )
        if not fired and selected:
            fired = True
            raise OSError(errno.EBADF, "bad descriptor")
        real_directory_fsync(descriptor)

    monkeypatch.setattr(publisher_module, "_fsync_file", fail_file_fsync)
    monkeypatch.setattr(publisher_module, "_fsync_directory", fail_directory_fsync)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64, card=True))

    assert fired
    assert raised.value.code in {
        ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
        ForgeErrorCode.EXTERNAL_MODIFICATION,
    }
    assert _visible(vault) == before
    _assert_no_transactions(vault)


def _run_hard_crashing_publish(vault: Path, crash_point: str) -> None:
    script = r"""
import os
from pathlib import Path

from cove_book_forge.config import ObsidianOutputConfig
from cove_book_forge.contracts import (
    AnalyzedChapter,
    BookMetadata,
    ChapterAnalysis,
    ChapterContent,
    ChapterSnapshot,
)
from cove_book_forge.contracts.analysis import Concept
from cove_book_forge.outputs.obsidian_render import ObsidianRenderer
from cove_book_forge.outputs.publisher import GuardedPublisher
import cove_book_forge.outputs.publisher as publisher_module

vault = Path(os.environ["COVE_CRASH_VAULT"])
crash_point = os.environ["COVE_CRASH_POINT"]
config = ObsidianOutputConfig(enabled=True, vault_path=vault)
snapshot = ChapterSnapshot(
    source_system="publisher-tests",
    external_book_id="book-atomic",
    book=BookMetadata(title="Atomic book", total_chapters=1),
    chapter=ChapterContent(index=0, title="Transaction", content="Private source."),
)
analyzed = AnalyzedChapter(
    input_fingerprint="b" * 64,
    cache_hit=True,
    analysis=ChapterAnalysis(
        core_idea="Bundle b",
        concepts=(Concept(term="Atomicity", definition="All or nothing."),),
    ),
)
renderer = ObsidianRenderer(config)

if crash_point == "first_backup":
    original = GuardedPublisher._backup
    fired = False

    def crash_after_backup(self, *args, **kwargs):
        global fired
        result = original(self, *args, **kwargs)
        if not fired:
            fired = True
            os._exit(86)
        return result

    GuardedPublisher._backup = crash_after_backup
elif crash_point in {"partial_publish", "manifest_link"}:
    original = GuardedPublisher._publish_stage
    non_manifest = 0

    def crash_after_publish(self, anchor, transaction, path, expected):
        global non_manifest
        result = original(self, anchor, transaction, path, expected)
        if path.endswith(".json"):
            if crash_point == "manifest_link":
                os._exit(86)
        else:
            non_manifest += 1
            if crash_point == "partial_publish" and non_manifest == 1:
                os._exit(86)
        return result

    GuardedPublisher._publish_stage = crash_after_publish
elif crash_point == "committed_cleanup":
    original = GuardedPublisher._unlink_identity_without_protection

    def crash_during_cleanup(directory_fd, name, expected):
        result = original(directory_fd, name, expected)
        if result and name.startswith("b"):
            os._exit(86)
        return result

    GuardedPublisher._unlink_identity_without_protection = staticmethod(crash_during_cleanup)
elif crash_point == "recovery_intent":
    original = GuardedPublisher._protect_moved

    def crash_after_recovery_intent(
        self, transaction, directory_fd, container, moved_name, original_path
    ):
        result = original(
            self, transaction, directory_fd, container, moved_name, original_path
        )
        if container == "transaction":
            os._exit(86)
        return result

    GuardedPublisher._protect_moved = crash_after_recovery_intent
else:
    raise SystemExit(99)

GuardedPublisher(config).publish(lambda previous: renderer.render(snapshot, analyzed, previous))
raise SystemExit(98)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env={
            **os.environ,
            "COVE_CRASH_POINT": crash_point,
            "COVE_CRASH_VAULT": str(vault),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 86, completed.stderr


@pytest.mark.parametrize(
    "crash_point",
    ["first_backup", "partial_publish", "manifest_link", "committed_cleanup"],
)
def test_hard_crash_is_recovered_before_the_next_manifest_read(
    tmp_path: Path, crash_point: str
) -> None:
    expected_vault = tmp_path / "expected"
    expected_vault.mkdir()
    _publish(expected_vault, _analyzed(card=True))
    _publish(expected_vault, _analyzed(fingerprint="b" * 64, card=True))
    expected = _visible(expected_vault)

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed(card=True))
    _run_hard_crashing_publish(vault, crash_point)

    transactions = vault / ".cove-book-forge" / ".transactions"
    assert transactions.is_dir()
    assert any(transactions.iterdir())
    for record in transactions.rglob("*.json"):
        assert str(vault) not in record.read_text("utf-8")

    renderer = ObsidianRenderer(ObsidianOutputConfig(enabled=True, vault_path=vault))

    def render_after_recovery(previous):
        assert not any(transactions.iterdir())
        return renderer.render(_snapshot(), _analyzed(fingerprint="b" * 64, card=True), previous)

    receipt = _publisher(vault).publish(render_after_recovery)

    assert receipt.rendered.manifest.checksum
    assert _visible(vault) == expected
    _assert_no_transactions(vault)
    retry = _publish(vault, _analyzed(fingerprint="b" * 64, card=True))
    assert retry.unchanged is True


def test_hard_crash_recovery_never_clobbers_or_adopts_a_competitor(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed(card=True))
    before = _visible(vault)
    _run_hard_crashing_publish(vault, "partial_publish")
    after_crash = _visible(vault)
    changed = sorted(path for path, data in after_crash.items() if before.get(path) != data)
    assert changed
    competitor_path = vault / changed[0]
    competitor = b"PRIVATE-RECOVERY-COMPETITOR"
    competitor_path.unlink()
    competitor_path.write_bytes(competitor)

    with pytest.raises(ForgeException) as raised:
        _publisher(vault).publish(lambda _previous: pytest.fail("renderer ran before recovery"))

    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert competitor_path.read_bytes() == competitor
    transactions = vault / ".cove-book-forge" / ".transactions"
    assert transactions.is_dir() and any(transactions.iterdir())
    for record in transactions.rglob("*.json"):
        public = record.read_text("utf-8")
        assert str(vault) not in public
        assert "PRIVATE-RECOVERY-COMPETITOR" not in public
    _assert_safe(raised.value, vault)


def test_restart_recovery_fsync_failure_preserves_old_bundle_and_durable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed(card=True))
    before = _visible(vault)
    _run_hard_crashing_publish(vault, "first_backup")
    real_fsync = publisher_module._fsync_directory
    fired = False

    def fail_first_recovery_fsync(descriptor: int) -> None:
        nonlocal fired
        if not fired:
            fired = True
            raise OSError(errno.EBADF, "bad recovery descriptor")
        real_fsync(descriptor)

    monkeypatch.setattr(publisher_module, "_fsync_directory", fail_first_recovery_fsync)
    with pytest.raises(ForgeException) as raised:
        _publisher(vault).publish(lambda _previous: pytest.fail("renderer ran before recovery"))

    assert fired
    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert _visible(vault) == before
    transactions = vault / ".cove-book-forge" / ".transactions"
    assert transactions.is_dir() and any(transactions.iterdir())

    monkeypatch.setattr(publisher_module, "_fsync_directory", real_fsync)
    recovered = _publish(vault, _analyzed(fingerprint="b" * 64, card=True))
    assert recovered.rendered.manifest.checksum
    _assert_no_transactions(vault)


def test_restart_recovery_can_resume_after_a_second_hard_crash(tmp_path: Path) -> None:
    expected_vault = tmp_path / "expected"
    expected_vault.mkdir()
    _publish(expected_vault, _analyzed(card=True))
    _publish(expected_vault, _analyzed(fingerprint="b" * 64, card=True))
    expected = _visible(expected_vault)

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed(card=True))
    _run_hard_crashing_publish(vault, "partial_publish")
    _run_hard_crashing_publish(vault, "recovery_intent")

    receipt = _publish(vault, _analyzed(fingerprint="b" * 64, card=True))

    assert receipt.rendered.manifest.checksum
    assert _visible(vault) == expected
    _assert_no_transactions(vault)


def test_transaction_removal_is_fsynced_in_its_parent_before_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    transactions = vault / ".cove-book-forge" / ".transactions"
    transactions.mkdir(parents=True)
    parent_identity = (transactions.stat().st_dev, transactions.stat().st_ino)
    real_fsync = publisher_module._fsync_directory
    removal_fsynced = False

    def record_parent_fsync(descriptor: int) -> None:
        nonlocal removal_fsynced
        identity = publisher_module._identity(os.fstat(descriptor))
        if identity == parent_identity and not any(transactions.iterdir()):
            removal_fsynced = True
        real_fsync(descriptor)

    monkeypatch.setattr(publisher_module, "_fsync_directory", record_parent_fsync)

    receipt = _publish(vault, _analyzed(card=True))

    assert receipt.unchanged is False
    assert removal_fsynced
    _assert_no_transactions(vault)


def test_post_commit_ebadf_never_reports_success_and_preserves_retry_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    expected_vault = tmp_path / "expected"
    expected_vault.mkdir()
    _publish(expected_vault, _analyzed(card=True))
    _publish(expected_vault, _analyzed(fingerprint="b" * 64, card=True))
    expected = _visible(expected_vault)

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed(card=True))
    real_cleanup = publisher_module.GuardedPublisher._cleanup_committed
    real_fsync = publisher_module._fsync_directory
    cleanup_active = False
    failures = 0

    def mark_cleanup(self, transaction) -> None:
        nonlocal cleanup_active
        cleanup_active = True
        try:
            real_cleanup(self, transaction)
        finally:
            cleanup_active = False

    def fail_committed_cleanup_fsync(descriptor: int) -> None:
        nonlocal failures
        if cleanup_active:
            failures += 1
            raise OSError(errno.EBADF, "bad committed-cleanup descriptor")
        real_fsync(descriptor)

    monkeypatch.setattr(publisher_module.GuardedPublisher, "_cleanup_committed", mark_cleanup)
    monkeypatch.setattr(publisher_module, "_fsync_directory", fail_committed_cleanup_fsync)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64, card=True))

    assert failures >= 2
    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert _visible(vault) == expected
    transactions = vault / ".cove-book-forge" / ".transactions"
    assert transactions.is_dir() and any(transactions.iterdir())
    assert list(transactions.rglob("state-committed.json"))
    _assert_safe(raised.value, vault)

    monkeypatch.setattr(publisher_module.GuardedPublisher, "_cleanup_committed", real_cleanup)
    monkeypatch.setattr(publisher_module, "_fsync_directory", real_fsync)
    retry = _publish(vault, _analyzed(fingerprint="b" * 64, card=True))
    assert retry.unchanged is True
    _assert_no_transactions(vault)


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


def test_link_success_followed_by_keyboard_interrupt_restores_exact_old_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    before = _visible(vault)
    real_link = publisher_module.os.link
    signal = KeyboardInterrupt("post-link transition")
    fired = False

    def link_then_interrupt(src, dst, **kwargs):
        nonlocal fired
        result = real_link(src, dst, **kwargs)
        if not fired and dst == Path(first.rendered.chapter_path).name:
            fired = True
            raise signal
        return result

    monkeypatch.setattr(publisher_module.os, "link", link_then_interrupt)

    with pytest.raises(KeyboardInterrupt) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value is signal
    assert fired
    assert _visible(vault) == before
    _assert_no_transactions(vault)


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


@pytest.mark.parametrize("failure", ["stage", "backup", "publish", "middle", "manifest"])
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
        real_rename = publisher_module._rename_noreplace
        fired = False

        def fail_rename(src, dst, **kwargs):
            nonlocal fired
            if not fired and isinstance(src, str) and not src.startswith("."):
                fired = True
                raise OSError("PRIVATE-BACKUP")
            return real_rename(src, dst, **kwargs)

        monkeypatch.setattr(publisher_module, "_rename_noreplace", fail_rename)
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
    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value.code in {
        ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
        ForgeErrorCode.EXTERNAL_MODIFICATION,
    }, "".join(traceback.format_tb(raised.tb))
    assert _visible(vault) == before
    _assert_no_transactions(vault)
    _assert_safe(raised.value, vault)


@pytest.mark.parametrize("failure", ["oserror", "keyboard_interrupt"])
def test_post_commit_backup_cleanup_failure_never_rolls_back_complete_new_bundle(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    expected_vault = tmp_path / "expected"
    expected_vault.mkdir()
    _publish(expected_vault, _analyzed())
    _publish(expected_vault, _analyzed(fingerprint="b" * 64))
    expected = _visible(expected_vault)

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    real_unlink = publisher_module.os.unlink
    signal = KeyboardInterrupt("committed backup cleanup")
    fired = False

    def unlink_then_fail(path, *args, **kwargs):
        nonlocal fired
        result = real_unlink(path, *args, **kwargs)
        if not fired and isinstance(path, str) and path.startswith("b"):
            fired = True
            if failure == "oserror":
                raise OSError("PRIVATE-COMMITTED-CLEANUP")
            raise signal
        return result

    monkeypatch.setattr(publisher_module.os, "unlink", unlink_then_fail)

    if failure == "oserror":
        receipt = _publish(vault, _analyzed(fingerprint="b" * 64))
        assert receipt.unchanged is False
    else:
        with pytest.raises(KeyboardInterrupt) as raised:
            _publish(vault, _analyzed(fingerprint="b" * 64))
        assert raised.value is signal

    assert fired
    assert _visible(vault) == expected


@pytest.mark.parametrize("cleanup_method", ["_close_transaction", "_cleanup_created"])
def test_post_commit_close_or_directory_cleanup_signal_keeps_complete_new_bundle(
    tmp_path: Path, monkeypatch, cleanup_method: str
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    expected_vault = tmp_path / "expected"
    expected_vault.mkdir()
    _publish(expected_vault, _analyzed())
    _publish(expected_vault, _analyzed(fingerprint="b" * 64))
    expected = _visible(expected_vault)

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    original = getattr(publisher_module.GuardedPublisher, cleanup_method)
    signal = SystemExit(f"committed {cleanup_method}")
    fired = False

    def cleanup_then_interrupt(self, *args, **kwargs):
        nonlocal fired
        result = original(self, *args, **kwargs)
        if not fired:
            fired = True
            raise signal
        return result

    monkeypatch.setattr(
        publisher_module.GuardedPublisher,
        cleanup_method,
        cleanup_then_interrupt,
    )

    with pytest.raises(SystemExit) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value is signal
    assert fired
    assert _visible(vault) == expected


def test_competitor_replacement_before_backup_is_preserved(tmp_path: Path, monkeypatch) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    chapter = vault / first.rendered.chapter_path
    competitor = b"competitor replacement"
    real_rename = publisher_module._rename_noreplace
    fired = False

    def race_rename(src, dst, **kwargs):
        nonlocal fired
        if not fired and src == chapter.name:
            fired = True
            chapter.unlink()
            chapter.write_bytes(competitor)
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr(publisher_module, "_rename_noreplace", race_rename)

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
    real_rename = publisher_module._rename_noreplace
    fired = False

    def rename_then_fail(src, dst, **kwargs):
        nonlocal fired
        result = real_rename(src, dst, **kwargs)
        if not fired and src == chapter.name:
            fired = True
            raise OSError("PRIVATE-POST-BACKUP-RENAME")
        return result

    monkeypatch.setattr(publisher_module, "_rename_noreplace", rename_then_fail)

    with pytest.raises(ForgeException):
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert fired
    assert _visible(vault) == before
    _assert_no_transactions(vault)


def test_backup_move_followed_by_internal_transition_signal_converges_to_old_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    before = _visible(vault)
    original = publisher_module.GuardedPublisher._snapshot_in_directory
    signal = KeyboardInterrupt("backup transition")
    fired = False

    def interrupt_first_backup_inspection(self, directory_fd, name):
        nonlocal fired
        if not fired and isinstance(name, str) and name.startswith("b"):
            fired = True
            raise signal
        return original(self, directory_fd, name)

    monkeypatch.setattr(
        publisher_module.GuardedPublisher,
        "_snapshot_in_directory",
        interrupt_first_backup_inspection,
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value is signal
    assert fired
    assert _visible(vault) == before
    _assert_no_transactions(vault)


def test_occupied_backup_destination_preserves_source_without_false_recovery_mapping(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    chapter = vault / first.rendered.chapter_path
    old_chapter = chapter.read_bytes()
    competitor = b"occupied backup destination"
    original = publisher_module._rename_noreplace
    fired = False

    def occupy_destination(source, destination, *, source_fd, destination_fd):
        nonlocal fired
        if not fired and source == chapter.name:
            fired = True
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination_fd,
            )
            try:
                os.write(descriptor, competitor)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return original(
            source,
            destination,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )

    monkeypatch.setattr(publisher_module, "_rename_noreplace", occupy_destination)

    with pytest.raises(ForgeException) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert fired
    assert raised.value.code is ForgeErrorCode.EXTERNAL_MODIFICATION
    assert chapter.read_bytes() == old_chapter
    transactions = vault / ".cove-book-forge" / ".transactions"
    competitors = [
        path for path in transactions.rglob("backup/b*") if path.read_bytes() == competitor
    ]
    assert len(competitors) == 1
    records = list(transactions.rglob("recovery-*.json"))
    assert all(
        json.loads(record.read_bytes())["moved_name"] != competitors[0].name for record in records
    )


def test_backup_noreplace_same_inode_collision_is_not_adopted_or_removed(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    chapter = vault / first.rendered.chapter_path
    old_chapter = chapter.read_bytes()
    original = publisher_module._rename_noreplace
    collision_name: str | None = None

    def hardlink_collision(source, destination, *, source_fd, destination_fd):
        nonlocal collision_name
        if collision_name is None and source == chapter.name and destination.startswith("b"):
            collision_name = destination
            os.link(
                source,
                destination,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
            return False
        return original(
            source,
            destination,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )

    monkeypatch.setattr(publisher_module, "_rename_noreplace", hardlink_collision)

    with pytest.raises(ForgeException):
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert collision_name is not None
    assert chapter.read_bytes() == old_chapter
    collision = next((vault / ".cove-book-forge" / ".transactions").rglob(collision_name))
    assert collision.read_bytes() == old_chapter
    assert collision.stat().st_ino == chapter.stat().st_ino
    records = list((vault / ".cove-book-forge" / ".transactions").rglob("recovery-*.json"))
    assert all(
        json.loads(record.read_bytes())["moved_name"] != collision_name for record in records
    )


def test_backup_noreplace_winner_is_never_restored_after_visible_source_disappears(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    chapter = vault / first.rendered.chapter_path
    winner = b"different occupied backup winner"
    original = publisher_module._rename_noreplace
    collision_name: str | None = None

    def collision_then_source_disappears(source, destination, *, source_fd, destination_fd):
        nonlocal collision_name
        if collision_name is None and source == chapter.name and destination.startswith("b"):
            collision_name = destination
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination_fd,
            )
            try:
                os.write(descriptor, winner)
            finally:
                os.close(descriptor)
            os.unlink(source, dir_fd=source_fd)
            return False
        return original(
            source,
            destination,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )

    monkeypatch.setattr(publisher_module, "_rename_noreplace", collision_then_source_disappears)

    with pytest.raises(ForgeException):
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert collision_name is not None
    assert not chapter.exists()
    collision = next((vault / ".cove-book-forge" / ".transactions").rglob(collision_name))
    assert collision.read_bytes() == winner
    records = list((vault / ".cove-book-forge" / ".transactions").rglob("recovery-*.json"))
    assert all(
        json.loads(record.read_bytes())["moved_name"] != collision_name for record in records
    )


@pytest.mark.parametrize("failure", ["keyboard_before", "oserror_after"])
def test_backup_no_move_retries_the_same_recovery_intent_until_fully_cleared(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    before = _visible(vault)
    chapter = vault / first.rendered.chapter_path
    competitor = b"backup forget collision"
    real_rename = publisher_module._rename_noreplace
    real_forget = publisher_module.GuardedPublisher._forget_moved
    signal = KeyboardInterrupt("backup no-move forget")
    collision_name: str | None = None
    forget_names: list[str] = []
    fault_fired = False

    def collide_backup(source, destination, *, source_fd, destination_fd):
        nonlocal collision_name
        if collision_name is None and source == chapter.name and destination.startswith("b"):
            collision_name = destination
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination_fd,
            )
            try:
                os.write(descriptor, competitor)
            finally:
                os.close(descriptor)
            return False
        return real_rename(
            source,
            destination,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )

    def interrupt_forget(self, transaction, directory_fd, moved_name):
        nonlocal fault_fired
        if collision_name is not None and moved_name == collision_name:
            forget_names.append(moved_name)
            if not fault_fired:
                fault_fired = True
                if failure == "keyboard_before":
                    raise signal
                real_forget(self, transaction, directory_fd, moved_name)
                raise OSError("PRIVATE-BACKUP-FORGET")
        return real_forget(self, transaction, directory_fd, moved_name)

    monkeypatch.setattr(publisher_module, "_rename_noreplace", collide_backup)
    monkeypatch.setattr(publisher_module.GuardedPublisher, "_forget_moved", interrupt_forget)

    expected_error = KeyboardInterrupt if failure == "keyboard_before" else ForgeException
    with pytest.raises(expected_error) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    if failure == "keyboard_before":
        assert raised.value is signal
    assert fault_fired
    assert collision_name is not None
    assert forget_names[:2] == [collision_name, collision_name]
    assert _visible(vault) == before
    collision = next((vault / ".cove-book-forge" / ".transactions").rglob(collision_name))
    assert collision.read_bytes() == competitor
    records = list((vault / ".cove-book-forge" / ".transactions").rglob("recovery-*.json"))
    assert all(
        json.loads(record.read_bytes())["moved_name"] != collision_name for record in records
    )


def test_backup_no_move_keeps_the_same_mapping_when_forget_cannot_clear_it(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    first = _publish(vault, _analyzed())
    before = _visible(vault)
    chapter = vault / first.rendered.chapter_path
    competitor = b"protected backup cleanup failure"
    real_rename = publisher_module._rename_noreplace
    real_forget = publisher_module.GuardedPublisher._forget_moved
    collision_name: str | None = None
    forget_names: list[str] = []

    def collide_backup(source, destination, *, source_fd, destination_fd):
        nonlocal collision_name
        if collision_name is None and source == chapter.name and destination.startswith("b"):
            collision_name = destination
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination_fd,
            )
            try:
                os.write(descriptor, competitor)
            finally:
                os.close(descriptor)
            return False
        return real_rename(
            source,
            destination,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )

    def fail_collision_forget(self, transaction, directory_fd, moved_name):
        if collision_name is not None and moved_name == collision_name:
            forget_names.append(moved_name)
            return False
        return real_forget(self, transaction, directory_fd, moved_name)

    monkeypatch.setattr(publisher_module, "_rename_noreplace", collide_backup)
    monkeypatch.setattr(publisher_module.GuardedPublisher, "_forget_moved", fail_collision_forget)

    with pytest.raises(ForgeException):
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert collision_name is not None
    assert len(forget_names) >= 2
    assert set(forget_names) == {collision_name}
    assert _visible(vault) == before
    records = [
        json.loads(record.read_bytes())
        for record in (vault / ".cove-book-forge" / ".transactions").rglob("recovery-*.json")
    ]
    assert [record["moved_name"] for record in records].count(collision_name) == 1


@pytest.mark.parametrize("failure", ["write", "fsync"])
def test_recovery_intent_persistence_failure_happens_before_backup_move(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    before = _visible(vault)
    real_write = publisher_module.os.write
    real_fsync = publisher_module._fsync_file
    recovery_written = False

    def fail_recovery_write(descriptor: int, data: bytes) -> int:
        nonlocal recovery_written
        if b'"recovery_id"' in data:
            if failure == "write":
                raise OSError("PRIVATE-RECOVERY-WRITE")
            recovery_written = True
        return real_write(descriptor, data)

    def fail_recovery_fsync(descriptor: int) -> None:
        nonlocal recovery_written
        if failure == "fsync" and recovery_written:
            recovery_written = False
            raise OSError("PRIVATE-RECOVERY-FSYNC")
        real_fsync(descriptor)

    monkeypatch.setattr(publisher_module.os, "write", fail_recovery_write)
    monkeypatch.setattr(publisher_module, "_fsync_file", fail_recovery_fsync)

    with pytest.raises(ForgeException):
        _publish(vault, _analyzed(fingerprint="b" * 64))

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
    real_rename = publisher_module._rename_noreplace
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
    monkeypatch.setattr(publisher_module, "_rename_noreplace", rollback_rename_then_fail)

    with pytest.raises(ForgeException):
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert manifest_failed and rollback_failed
    assert _visible(vault) == before
    _assert_no_transactions(vault)


@pytest.mark.parametrize(
    "kind",
    ["source_hardlink", "regular", "source_absent", "directory", "symlink", "fifo"],
)
def test_rollback_noreplace_collision_never_adopts_or_deletes_destination(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    before = _visible(vault)
    real_link = publisher_module.os.link
    real_rename = publisher_module._rename_noreplace
    competitor = b"rollback collision winner"
    manifest_failed = False
    collision_name: str | None = None

    def fail_manifest_once(src, dst, **kwargs):
        nonlocal manifest_failed
        if not manifest_failed and isinstance(dst, str) and dst.endswith(".json"):
            manifest_failed = True
            raise OSError("PRIVATE-MANIFEST-FAILURE")
        return real_link(src, dst, **kwargs)

    def collide_once(source, destination, *, source_fd, destination_fd):
        nonlocal collision_name
        source_exists = True
        try:
            os.stat(source, dir_fd=source_fd, follow_symlinks=False)
        except FileNotFoundError:
            source_exists = False
        if collision_name is None and destination.startswith("r") and source_exists:
            collision_name = destination
            if kind == "source_hardlink":
                os.link(
                    source,
                    destination,
                    src_dir_fd=source_fd,
                    dst_dir_fd=destination_fd,
                    follow_symlinks=False,
                )
            elif kind in {"regular", "source_absent"}:
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=destination_fd,
                )
                try:
                    os.write(descriptor, competitor)
                finally:
                    os.close(descriptor)
                if kind == "source_absent":
                    os.unlink(source, dir_fd=source_fd)
            elif kind == "directory":
                os.mkdir(destination, 0o700, dir_fd=destination_fd)
            elif kind == "symlink":
                os.symlink("opaque-rollback-winner", destination, dir_fd=destination_fd)
            else:
                os.mkfifo(destination, 0o600, dir_fd=destination_fd)
            os.stat(destination, dir_fd=destination_fd, follow_symlinks=False)
            return False
        return real_rename(
            source,
            destination,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )

    monkeypatch.setattr(publisher_module.os, "link", fail_manifest_once)
    monkeypatch.setattr(publisher_module, "_rename_noreplace", collide_once)

    with pytest.raises(ForgeException):
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert manifest_failed
    assert collision_name is not None
    assert _visible(vault) == before
    matches = list((vault / ".cove-book-forge" / ".transactions").rglob(collision_name))
    assert len(matches) == 1
    collision = matches[0]
    status = collision.lstat()
    if kind in {"source_hardlink", "regular", "source_absent"}:
        assert stat.S_ISREG(status.st_mode)
        expected = competitor if kind != "source_hardlink" else None
        if expected is not None:
            assert collision.read_bytes() == expected
    elif kind == "directory":
        assert stat.S_ISDIR(status.st_mode)
    elif kind == "symlink":
        assert stat.S_ISLNK(status.st_mode)
        assert os.readlink(collision) == "opaque-rollback-winner"
    else:
        assert stat.S_ISFIFO(status.st_mode)
    records = list((vault / ".cove-book-forge" / ".transactions").rglob("recovery-*.json"))
    assert all(
        json.loads(record.read_bytes())["moved_name"] != collision_name for record in records
    )


def test_rollback_ambiguous_exception_with_source_and_hardlink_destination_is_no_move(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    before = _visible(vault)
    real_link = publisher_module.os.link
    real_rename = publisher_module._rename_noreplace
    signal = KeyboardInterrupt("rollback hardlink collision")
    manifest_failed = False
    collision_name: str | None = None

    def fail_manifest_once(src, dst, **kwargs):
        nonlocal manifest_failed
        if not manifest_failed and isinstance(dst, str) and dst.endswith(".json"):
            manifest_failed = True
            raise OSError("PRIVATE-MANIFEST-FAILURE")
        return real_link(src, dst, **kwargs)

    def hardlink_then_interrupt(source, destination, *, source_fd, destination_fd):
        nonlocal collision_name
        try:
            os.stat(source, dir_fd=source_fd, follow_symlinks=False)
        except FileNotFoundError:
            source_exists = False
        else:
            source_exists = True
        if collision_name is None and destination.startswith("r") and source_exists:
            collision_name = destination
            os.link(
                source,
                destination,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
            raise signal
        return real_rename(
            source,
            destination,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )

    monkeypatch.setattr(publisher_module.os, "link", fail_manifest_once)
    monkeypatch.setattr(publisher_module, "_rename_noreplace", hardlink_then_interrupt)

    with pytest.raises(KeyboardInterrupt) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert raised.value is signal
    assert manifest_failed
    assert collision_name is not None
    assert _visible(vault) == before
    collision = next((vault / ".cove-book-forge" / ".transactions").rglob(collision_name))
    assert stat.S_ISREG(collision.lstat().st_mode)
    records = list((vault / ".cove-book-forge" / ".transactions").rglob("recovery-*.json"))
    assert all(
        json.loads(record.read_bytes())["moved_name"] != collision_name for record in records
    )


@pytest.mark.parametrize("failure", ["keyboard_before", "oserror_after"])
def test_rollback_no_move_retries_the_same_recovery_intent_until_fully_cleared(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    before = _visible(vault)
    real_link = publisher_module.os.link
    real_rename = publisher_module._rename_noreplace
    real_forget = publisher_module.GuardedPublisher._forget_moved
    signal = KeyboardInterrupt("rollback no-move forget")
    competitor = b"rollback forget collision"
    manifest_failed = False
    collision_name: str | None = None
    forget_names: list[str] = []
    fault_fired = False

    def fail_manifest_once(src, dst, **kwargs):
        nonlocal manifest_failed
        if not manifest_failed and isinstance(dst, str) and dst.endswith(".json"):
            manifest_failed = True
            raise OSError("PRIVATE-MANIFEST-FAILURE")
        return real_link(src, dst, **kwargs)

    def collide_rollback(source, destination, *, source_fd, destination_fd):
        nonlocal collision_name
        try:
            os.stat(source, dir_fd=source_fd, follow_symlinks=False)
        except FileNotFoundError:
            source_exists = False
        else:
            source_exists = True
        if collision_name is None and destination.startswith("r") and source_exists:
            collision_name = destination
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination_fd,
            )
            try:
                os.write(descriptor, competitor)
            finally:
                os.close(descriptor)
            return False
        return real_rename(
            source,
            destination,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )

    def interrupt_forget(self, transaction, directory_fd, moved_name):
        nonlocal fault_fired
        if collision_name is not None and moved_name == collision_name:
            forget_names.append(moved_name)
            if not fault_fired:
                fault_fired = True
                if failure == "keyboard_before":
                    raise signal
                real_forget(self, transaction, directory_fd, moved_name)
                raise OSError("PRIVATE-ROLLBACK-FORGET")
        return real_forget(self, transaction, directory_fd, moved_name)

    monkeypatch.setattr(publisher_module.os, "link", fail_manifest_once)
    monkeypatch.setattr(publisher_module, "_rename_noreplace", collide_rollback)
    monkeypatch.setattr(publisher_module.GuardedPublisher, "_forget_moved", interrupt_forget)

    expected_error = KeyboardInterrupt if failure == "keyboard_before" else ForgeException
    with pytest.raises(expected_error) as raised:
        _publish(vault, _analyzed(fingerprint="b" * 64))

    if failure == "keyboard_before":
        assert raised.value is signal
    assert manifest_failed and fault_fired
    assert collision_name is not None
    assert forget_names[:2] == [collision_name, collision_name]
    assert _visible(vault) == before
    collision = next((vault / ".cove-book-forge" / ".transactions").rglob(collision_name))
    assert collision.read_bytes() == competitor
    records = list((vault / ".cove-book-forge" / ".transactions").rglob("recovery-*.json"))
    assert all(
        json.loads(record.read_bytes())["moved_name"] != collision_name for record in records
    )


def test_rollback_no_move_keeps_same_mapping_and_visible_file_when_forget_stays_failed(
    tmp_path: Path, monkeypatch
) -> None:
    import cove_book_forge.outputs.publisher as publisher_module

    vault = tmp_path / "vault"
    vault.mkdir()
    _publish(vault, _analyzed())
    real_link = publisher_module.os.link
    real_rename = publisher_module._rename_noreplace
    real_forget = publisher_module.GuardedPublisher._forget_moved
    competitor = b"protected rollback cleanup failure"
    manifest_failed = False
    collision_name: str | None = None
    published_path: str | None = None
    published_bytes: bytes | None = None
    forget_names: list[str] = []

    def fail_manifest_once(src, dst, **kwargs):
        nonlocal manifest_failed
        if not manifest_failed and isinstance(dst, str) and dst.endswith(".json"):
            manifest_failed = True
            raise OSError("PRIVATE-MANIFEST-FAILURE")
        return real_link(src, dst, **kwargs)

    def collide_rollback(source, destination, *, source_fd, destination_fd):
        nonlocal collision_name, published_bytes
        source_exists = False
        if collision_name is None and destination.startswith("r"):
            try:
                descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd)
            except FileNotFoundError:
                pass
            else:
                source_exists = True
                try:
                    size = os.fstat(descriptor).st_size
                    published_bytes = os.read(descriptor, size)
                finally:
                    os.close(descriptor)
        if collision_name is None and destination.startswith("r") and source_exists:
            collision_name = destination
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=destination_fd,
            )
            try:
                os.write(descriptor, competitor)
            finally:
                os.close(descriptor)
            return False
        return real_rename(
            source,
            destination,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )

    def fail_collision_forget(self, transaction, directory_fd, moved_name):
        nonlocal published_path
        if collision_name is not None and moved_name == collision_name:
            forget_names.append(moved_name)
            recovery = transaction.recoveries[(directory_fd, moved_name)]
            published_path = recovery.original_path
            return False
        return real_forget(self, transaction, directory_fd, moved_name)

    monkeypatch.setattr(publisher_module.os, "link", fail_manifest_once)
    monkeypatch.setattr(publisher_module, "_rename_noreplace", collide_rollback)
    monkeypatch.setattr(publisher_module.GuardedPublisher, "_forget_moved", fail_collision_forget)

    with pytest.raises(ForgeException):
        _publish(vault, _analyzed(fingerprint="b" * 64))

    assert manifest_failed
    assert collision_name is not None
    assert published_path is not None
    assert published_bytes is not None
    assert len(forget_names) >= 2
    assert set(forget_names) == {collision_name}
    assert (vault / published_path).read_bytes() == published_bytes
    collision = next((vault / ".cove-book-forge" / ".transactions").rglob(collision_name))
    assert collision.read_bytes() == competitor
    records = [
        json.loads(record.read_bytes())
        for record in (vault / ".cove-book-forge" / ".transactions").rglob("recovery-*.json")
    ]
    assert [record["moved_name"] for record in records].count(collision_name) == 1


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
    real_rename = publisher_module._rename_noreplace
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

    monkeypatch.setattr(publisher_module, "_rename_noreplace", replace_then_rename)

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
