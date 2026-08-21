from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from test_skill_render import _render

from cove_book_forge.config import SkillOutputConfig
from cove_book_forge.contracts import SkillInstallResult
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs import skill_install as skill_install_module
from cove_book_forge.outputs.skill_install import SkillInstaller
from cove_book_forge.outputs.skill_publisher import CanonicalSkillPublisher


def _published(root: Path):
    root.mkdir()
    config = SkillOutputConfig(enabled=True, canonical_path=root)
    return config, CanonicalSkillPublisher(config).publish(_render())


@pytest.mark.parametrize(
    ("target", "relative_root"),
    (
        ("agents", Path(".agents/skills")),
        ("codex", Path(".codex/skills")),
        ("claude", Path(".claude/skills")),
    ),
)
def test_selected_standard_root_gets_a_relative_symlink_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    relative_root: Path,
) -> None:
    """Changing a conventional root or rewriting a correct link must fail this test."""
    home = tmp_path / "home"
    install_root = home / relative_root
    install_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    config, receipt = _published(tmp_path / "canonical")
    config = config.model_copy(update={"install_to": (target,)})

    first = SkillInstaller(config).install(receipt)
    installed = install_root / receipt.rendered.skill_slug
    first_identity = installed.lstat().st_ino
    expected_link = os.path.relpath(
        config.canonical_path / receipt.rendered.skill_slug,
        start=install_root,
    )

    assert first == (
        SkillInstallResult(
            target=target,
            path=(relative_root / receipt.rendered.skill_slug).as_posix(),
            strategy="symlink",
            unchanged=False,
        ),
    )
    assert installed.is_symlink()
    assert os.readlink(installed) == expected_link
    assert SkillInstaller(config).install(receipt)[0].unchanged is True
    assert installed.lstat().st_ino == first_identity


def test_symlink_unsupported_uses_a_verified_managed_copy_and_reuses_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falling back without an unsupported-symlink error or recopying must fail this test."""
    home = tmp_path / "home"
    install_root = home / ".codex/skills"
    install_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    config, receipt = _published(tmp_path / "canonical")
    config = config.model_copy(update={"install_to": ("codex",)})

    def unsupported(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EOPNOTSUPP, "symlinks unsupported")

    monkeypatch.setattr(skill_install_module, "_create_symlink", unsupported)
    first = SkillInstaller(config).install(receipt)[0]
    installed = install_root / receipt.rendered.skill_slug
    first_manifest_identity = (installed / ".cove-book-forge.json").stat().st_ino

    assert first.strategy == "copy"
    assert first.unchanged is False
    assert installed.is_dir() and not installed.is_symlink()
    assert (installed / ".cove-book-forge-install.json").is_file()
    second = SkillInstaller(config).install(receipt)[0]
    assert second.strategy == "copy"
    assert second.unchanged is True
    assert (installed / ".cove-book-forge.json").stat().st_ino == first_manifest_identity


@pytest.mark.parametrize("occupied_kind", ("file", "directory", "link"))
def test_existing_nonmanaged_target_is_preserved_as_install_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    occupied_kind: str,
) -> None:
    """Adopting or replacing an unowned entry must fail this test."""
    home = tmp_path / "home"
    install_root = home / ".agents/skills"
    install_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    config, receipt = _published(tmp_path / "canonical")
    config = config.model_copy(update={"install_to": ("agents",)})
    occupied = install_root / receipt.rendered.skill_slug
    if occupied_kind == "file":
        occupied.write_text("competitor", encoding="utf-8")
    elif occupied_kind == "directory":
        occupied.mkdir()
        (occupied / "competitor.txt").write_text("keep", encoding="utf-8")
    else:
        os.symlink("elsewhere", occupied)

    before = occupied.lstat()
    with pytest.raises(ForgeException) as error:
        SkillInstaller(config).install(receipt)

    assert error.value.code is ForgeErrorCode.INSTALL_CONFLICT
    assert str(home) not in str(error.value)
    assert occupied.lstat().st_ino == before.st_ino
    assert (config.canonical_path / receipt.rendered.skill_slug).is_symlink()
