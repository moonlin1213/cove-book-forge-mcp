"""Public documentation and example coverage for generated Agent Skills."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_documents_completed_single_chapter_skill_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for expected in (
        "Generated Agent Skills",
        "`$<skill-slug>`",
        "natural-language request",
        "The canonical root remains the source of",
        "relative symlink",
        "verified managed copy",
        "INSTALL_CONFLICT",
        "byte-for-byte no-op",
        "`ChapterSnapshot`",
        "[book-to-skill](https://github.com/virgiliojr94/book-to-skill)",
        "**Virgilio Jr.**",
    ):
        assert expected in readme


def test_publish_chapter_skill_example_runs_without_a_provider() -> None:
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}

    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "publish_chapter_skill.py")],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Published ")
