"""Verify release archives contain no private workspace artifacts."""

from __future__ import annotations

import argparse
import sys
import tarfile
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath

FORBIDDEN_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".superpowers",
        ".tox",
        ".venv",
        ".worktrees",
        "__pycache__",
        "build",
        "data",
        "dist",
        "generated-skills",
        "imports",
        "venv",
    }
)
FORBIDDEN_FILES = frozenset({"config.yaml"})


def _archive_files(distribution_dir: Path) -> tuple[Path, Path]:
    wheels = tuple(distribution_dir.glob("*.whl"))
    source_distributions = tuple(distribution_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(source_distributions) != 1:
        raise ValueError(
            "expected exactly one wheel and one source distribution in "
            f"{distribution_dir}"
        )
    return wheels[0], source_distributions[0]


def _zip_members(path: Path) -> Iterator[tuple[str, bytes]]:
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            if not member.is_dir():
                yield member.filename, archive.read(member)


def _tar_members(path: Path) -> Iterator[tuple[str, bytes]]:
    with tarfile.open(path, mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"could not read archive member: {member.name}")
            yield member.name, extracted.read()


def _members(path: Path) -> Iterator[tuple[str, bytes]]:
    if path.suffix == ".whl":
        yield from _zip_members(path)
        return
    yield from _tar_members(path)


def _forbidden_member_reason(name: str) -> str | None:
    parts = PurePosixPath(name).parts
    for part in parts:
        if part in FORBIDDEN_DIRECTORIES:
            return f"forbidden directory {part!r}"
        if part == ".coverage" or part.startswith(".coverage."):
            return "coverage data"
        if part in FORBIDDEN_FILES:
            return f"local file {part!r}"
        if (part == ".env" or part.startswith(".env.")) and part != ".env.example":
            return "environment file"
        if part.endswith((".pyc", ".pyo")):
            return "Python bytecode"
    return None


def verify_archives(distribution_dir: Path, repository_root: Path) -> list[str]:
    failures: list[str] = []
    repository_marker = str(repository_root.resolve()).encode()
    try:
        archives = _archive_files(distribution_dir)
    except ValueError as exc:
        return [str(exc)]

    for archive in archives:
        for name, content in _members(archive):
            reason = _forbidden_member_reason(name)
            if reason is not None:
                failures.append(f"{archive.name}: {name}: {reason}")
            if repository_marker in content:
                failures.append(f"{archive.name}: {name}: contains the absolute repository path")
    return failures


def _format_failures(failures: Iterable[str]) -> str:
    return "\n".join(f"- {failure}" for failure in failures)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("distribution_dir", nargs="?", type=Path, default=Path("dist"))
    args = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    failures = verify_archives(args.distribution_dir, repository_root)
    if failures:
        print("Distribution verification failed:\n" + _format_failures(failures), file=sys.stderr)
        return 1
    print("Distribution verification passed for one wheel and one source distribution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
