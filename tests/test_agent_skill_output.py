from __future__ import annotations

from pathlib import Path

import pytest
from test_skill_render import _analyzed, _snapshot

from cove_book_forge.config import SkillOutputConfig
from cove_book_forge.contracts import SkillPublishResult
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.outputs import AgentSkillOutput


def test_public_output_publishes_canonical_content_then_selected_installations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping canonical publication or leaking an absolute result path must fail this test."""
    home = tmp_path / "home"
    (home / ".codex/skills").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    config = SkillOutputConfig(
        enabled=True,
        canonical_path=canonical,
        install_to=("codex",),
    )

    result = AgentSkillOutput(config).publish(_snapshot(), _analyzed())

    assert isinstance(result, SkillPublishResult)
    assert result.canonical_path == result.skill_slug
    assert result.installations[0].path == f".codex/skills/{result.skill_slug}"
    assert not Path(result.canonical_path).is_absolute()
    assert (canonical / result.skill_slug).is_symlink()
    assert (home / result.installations[0].path).is_symlink()


def test_install_conflict_is_reported_after_canonical_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rolling back canonical output or changing the fixed conflict code must fail this test."""
    home = tmp_path / "home"
    install_root = home / ".claude/skills"
    install_root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    config = SkillOutputConfig(
        enabled=True,
        canonical_path=canonical,
        install_to=("claude",),
    )
    snapshot = _snapshot()
    analyzed = _analyzed()
    slug = AgentSkillOutput(config).publish(snapshot, analyzed).skill_slug
    installed = install_root / slug
    installed.unlink()
    installed.mkdir()
    (installed / "competitor").write_text("keep", encoding="utf-8")

    changed = _analyzed(fingerprint="b" * 64)
    with pytest.raises(ForgeException) as error:
        AgentSkillOutput(config).publish(snapshot, changed)

    assert error.value.code is ForgeErrorCode.INSTALL_CONFLICT
    assert (canonical / slug).is_symlink()
    assert (installed / "competitor").read_text(encoding="utf-8") == "keep"
