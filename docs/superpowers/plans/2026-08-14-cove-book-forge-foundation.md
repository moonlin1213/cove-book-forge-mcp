# Cove Book Forge Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the installable, attributed, typed foundation of `cove-book-forge-mcp`, including public contracts, configuration, authorized-path validation, structured errors, and a read-only `doctor` CLI.

**Architecture:** This first delivery creates dependency-free domain contracts at the center, with configuration and diagnostics depending on them but no MCP, model, parser, database, or output implementation yet. Later plans consume these exact types; this plan therefore locks public names and validation semantics before stateful work begins.

**Tech Stack:** Python 3.11+, uv, hatchling, Pydantic 2, PyYAML, platformdirs, Typer, pytest, pytest-cov, Ruff, mypy.

## Global Constraints

- The package name is `cove-book-forge-mcp`; the import package is `cove_book_forge`.
- The project has no official UI and no dependency on Cove/栖渡 private code.
- Python support is 3.11 through 3.14.
- Default behavior is local-first, no telemetry, no cloud sync, and no remote logging.
- API keys are never stored in the YAML configuration; only environment-variable names are stored.
- Public contracts contain no `Moon`, `栖渡`, or private Cove-specific fields.
- All output roots require explicit configuration and must later pass realpath containment checks.
- Tests and examples may use only original, public-domain, or explicitly open-licensed text.
- The repository uses the MIT License.
- README, acknowledgements, third-party notices, changelog, and the first release must thank Virgilio Jr. and link to `https://github.com/virgiliojr94/book-to-skill`.
- Any upstream code copied or modified in later plans must retain the upstream copyright and MIT notice.
- Every task follows red-green-refactor and ends in a focused commit.

## Locked File Structure

```text
pyproject.toml
README.md
LICENSE
ACKNOWLEDGEMENTS.md
THIRD_PARTY_NOTICES.md
CHANGELOG.md
.gitignore
.github/workflows/ci.yml
src/cove_book_forge/
├── __init__.py
├── __main__.py
├── py.typed
├── cli.py
├── doctor.py
├── errors.py
├── contracts/
│   ├── __init__.py
│   ├── analysis.py
│   ├── base.py
│   ├── books.py
│   └── jobs.py
└── config/
    ├── __init__.py
    ├── loader.py
    ├── models.py
    └── paths.py
tests/
├── test_attribution.py
├── test_cli_doctor.py
├── test_config.py
├── test_contract_analysis.py
├── test_contract_books.py
├── test_contract_jobs.py
├── test_errors.py
├── test_package.py
└── test_paths.py
```

Later plans must add modules beside these rather than expanding these files into unrelated responsibilities.

---

### Task 1: Bootstrap the Package and Preserve Attribution

**Files:**
- Create: `pyproject.toml`
- Create: `src/cove_book_forge/__init__.py`
- Create: `src/cove_book_forge/py.typed`
- Create: `tests/test_package.py`
- Create: `tests/test_attribution.py`
- Create: `README.md`
- Create: `LICENSE`
- Create: `ACKNOWLEDGEMENTS.md`
- Create: `THIRD_PARTY_NOTICES.md`
- Create: `CHANGELOG.md`
- Create: `.gitignore`

**Interfaces:**
- Produces: `cove_book_forge.__version__: str` with value `0.1.0.dev0`.
- Produces: console entry point name `cove-book-forge`, implemented in Task 6.
- Produces: repository-wide dependency and quality configuration consumed by all later tasks.

- [ ] **Step 1: Add packaging metadata and failing package tests**

Create `pyproject.toml` with these effective settings:

```toml
[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[project]
name = "cove-book-forge-mcp"
version = "0.1.0.dev0"
description = "Local-first MCP server that forges books into Obsidian knowledge and reusable Agent Skills."
readme = "README.md"
requires-python = ">=3.11"
license = { text = "MIT" }
dependencies = [
  "platformdirs>=4.3,<5",
  "pydantic>=2.11,<3",
  "PyYAML>=6.0,<7",
  "typer>=0.16,<1",
]

[project.scripts]
cove-book-forge = "cove_book_forge.cli:app"

[dependency-groups]
dev = [
  "mypy>=1.17,<2",
  "pytest>=8.4,<10",
  "pytest-cov>=6,<8",
  "ruff>=0.12,<1",
]

[tool.hatch.build.targets.wheel]
packages = ["src/cove_book_forge"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra --strict-markers"

[tool.ruff]
target-version = "py311"
line-length = 100

[tool.ruff.lint]
select = ["E4", "E7", "E9", "F", "I", "UP", "B", "SIM"]

[tool.mypy]
python_version = "3.11"
strict = true
packages = ["cove_book_forge"]
```

Create `tests/test_package.py`:

```python
from cove_book_forge import __version__


def test_package_version_matches_first_development_release() -> None:
    assert __version__ == "0.1.0.dev0"
```

- [ ] **Step 2: Sync dependencies and verify the package test fails**

Run:

```bash
uv sync --group dev --no-install-project
uv run --no-sync pytest tests/test_package.py -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'cove_book_forge'`.

- [ ] **Step 3: Add the minimal import package**

Create `src/cove_book_forge/__init__.py`:

```python
"""Public package for cove-book-forge-mcp."""

__version__ = "0.1.0.dev0"

__all__ = ["__version__"]
```

Create an empty `src/cove_book_forge/py.typed` marker.

- [ ] **Step 4: Add attribution tests before the documents**

Create `tests/test_attribution.py`:

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_URL = "https://github.com/virgiliojr94/book-to-skill"


def test_readme_and_acknowledgements_credit_book_to_skill() -> None:
    for name in ("README.md", "ACKNOWLEDGEMENTS.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "Virgilio Jr." in text
        assert UPSTREAM_URL in text


def test_third_party_notice_preserves_upstream_copyright() -> None:
    text = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "Copyright (c) 2025 virgiliojr94" in text
    assert "MIT License" in text
```

- [ ] **Step 5: Run attribution tests and verify they fail**

Run:

```bash
uv run pytest tests/test_attribution.py -v
```

Expected: FAIL because the public documentation files do not exist.

- [ ] **Step 6: Add the public license and attribution documents**

Create `LICENSE` with this complete text:

```text
MIT License

Copyright (c) 2026 cove-book-forge-mcp contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Create `ACKNOWLEDGEMENTS.md` containing this exact acknowledgement:

```markdown
# Acknowledgements

`cove-book-forge-mcp` is inspired by and builds upon ideas and tooling from
[book-to-skill](https://github.com/virgiliojr94/book-to-skill), created by
Virgilio Jr.

We are grateful for the project's document extraction work, Agent Skill
structure, and open-source contribution.
```

Create `THIRD_PARTY_NOTICES.md` with this notice and complete upstream license:

```text
# Third-Party Notices

## book-to-skill

Repository: https://github.com/virgiliojr94/book-to-skill
Author: Virgilio Jr.

This project is inspired by book-to-skill's document extraction work and
Agent Skill structure. Any source files copied or modified from the upstream
project will be listed in this section when they are introduced.

MIT License

Copyright (c) 2025 virgiliojr94

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

Create `README.md` with this initial content:

```markdown
# cove-book-forge-mcp

A local-first, headless MCP server for turning PDF/EPUB books and external
reading-system snapshots into Obsidian knowledge and reusable Agent Skills.

This repository contains the independent open-source core. It does not ship an
official reading UI and does not depend on private Cove/栖渡 code. A later MCP
phase will let existing reading systems submit stable snapshots, while an
optional managed library will serve users who do not already have one.

> Status: early development. The current foundation establishes public
> contracts, safe configuration, and diagnostics before parser, provider,
> output, job, and MCP phases are added.

## Privacy defaults

- local library and generated files stay local;
- telemetry, cloud sync, and remote logging are disabled;
- API-key values come from environment variables and are never stored in YAML;
- all output roots require explicit configuration.

## Acknowledgements

`cove-book-forge-mcp` is inspired by and builds upon ideas and tooling from
[book-to-skill](https://github.com/virgiliojr94/book-to-skill), created by
Virgilio Jr. We are grateful for its document extraction work, Agent Skill
structure, and open-source contribution.

## License

MIT. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
```

Create `CHANGELOG.md` with an `Unreleased` section containing:

```markdown
- Established the independent project design and public attribution to
  [book-to-skill](https://github.com/virgiliojr94/book-to-skill) by Virgilio Jr.
```

Create `.gitignore` with these repository-local exclusions:

```gitignore
.venv/
.pytest_cache/
.mypy_cache/
.ruff_cache/
__pycache__/
*.pyc
.coverage
htmlcov/
dist/
build/
*.egg-info/
.DS_Store
.env
.env.*
!.env.example
config.yaml
data/
imports/
generated-skills/
tests/fixtures/local-obsidian-vault/
```

- [ ] **Step 7: Run the bootstrap tests and quality checks**

Run:

```bash
uv sync --group dev
uv run pytest tests/test_package.py tests/test_attribution.py -v
uv run ruff check .
uv run mypy src
git diff --check
```

Expected: all commands pass.

- [ ] **Step 8: Commit the bootstrap**

```bash
git add pyproject.toml uv.lock README.md LICENSE ACKNOWLEDGEMENTS.md \
  THIRD_PARTY_NOTICES.md CHANGELOG.md .gitignore src/cove_book_forge \
  tests/test_package.py tests/test_attribution.py
git commit -m "chore: bootstrap project and attribution"
```

---

### Task 2: Add Structured Errors and Book Contracts

**Files:**
- Create: `src/cove_book_forge/errors.py`
- Create: `src/cove_book_forge/contracts/__init__.py`
- Create: `src/cove_book_forge/contracts/base.py`
- Create: `src/cove_book_forge/contracts/books.py`
- Create: `tests/test_errors.py`
- Create: `tests/test_contract_books.py`

**Interfaces:**
- Produces: `ForgeErrorCode`, `ForgeErrorDetail`, and `ForgeException`.
- Produces: `ContractModel`, the immutable strict base for every public contract.
- Produces: `ExternalIdentity`, `BookMetadata`, `BookRef`, `ChapterContent`, `Highlight`, `UserNote`, `Annotation`, `Reflection`, and `ChapterSnapshot`.
- All Pydantic contract models reject unknown fields with `extra="forbid"`.

- [ ] **Step 1: Write failing structured-error tests**

Create `tests/test_errors.py`:

```python
from cove_book_forge.errors import ForgeErrorCode, ForgeException


def test_forge_exception_serializes_public_error_without_private_cause() -> None:
    exc = ForgeException(
        ForgeErrorCode.CONFIG_INVALID,
        "Configuration is invalid.",
        retryable=False,
        details={"field": "model.provider"},
        cause=RuntimeError("Authorization: Bearer secret"),
    )

    assert exc.as_result() == {
        "ok": False,
        "error": {
            "code": "CONFIG_INVALID",
            "message": "Configuration is invalid.",
            "retryable": False,
            "details": {"field": "model.provider"},
        },
    }
    assert "secret" not in str(exc.as_result())
```

- [ ] **Step 2: Run the error test and verify it fails**

Run:

```bash
uv run pytest tests/test_errors.py -v
```

Expected: FAIL because `cove_book_forge.errors` does not exist.

- [ ] **Step 3: Implement the error contract**

Implement `ForgeErrorCode` as a `StrEnum` containing every code from design section 14:

```python
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ForgeErrorCode(StrEnum):
    CONFIG_INVALID = "CONFIG_INVALID"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
    MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
    MODEL_OUTPUT_INVALID = "MODEL_OUTPUT_INVALID"
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    ENCRYPTED_DOCUMENT = "ENCRYPTED_DOCUMENT"
    OCR_REQUIRED = "OCR_REQUIRED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    EXTERNAL_BOOK_INCOMPLETE = "EXTERNAL_BOOK_INCOMPLETE"
    OUTPUT_NOT_CONFIGURED = "OUTPUT_NOT_CONFIGURED"
    OUTPUT_PERMISSION_DENIED = "OUTPUT_PERMISSION_DENIED"
    EXTERNAL_MODIFICATION = "EXTERNAL_MODIFICATION"
    INSTALL_CONFLICT = "INSTALL_CONFLICT"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    JOB_CONFLICT = "JOB_CONFLICT"
    JOB_INTERRUPTED = "JOB_INTERRUPTED"
    JOB_CANCELLED = "JOB_CANCELLED"


class ForgeErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ForgeErrorCode
    message: str = Field(min_length=1, max_length=1200)
    retryable: bool = False
    details: dict[str, JsonValue] = Field(default_factory=dict)


class ForgeException(RuntimeError):
    def __init__(
        self,
        code: ForgeErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, JsonValue] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}
        self.__cause__ = cause

    def as_result(self) -> dict[str, object]:
        detail = ForgeErrorDetail(
            code=self.code,
            message=str(self),
            retryable=self.retryable,
            details=self.details,
        )
        return {"ok": False, "error": detail.model_dump(mode="json")}
```

Import `JsonValue` from `pydantic`. The private cause is only chained through `__cause__`; it is never serialized by `as_result()`.

- [ ] **Step 4: Write failing book-contract tests**

Create `tests/test_contract_books.py` with these cases:

```python
import pytest
from pydantic import ValidationError

from cove_book_forge.contracts.books import (
    BookMetadata,
    ChapterContent,
    ChapterSnapshot,
    ExternalIdentity,
    Highlight,
)


def test_external_snapshot_keeps_generic_reading_context() -> None:
    snapshot = ChapterSnapshot(
        source_system="reader",
        external_book_id="book-1",
        book=BookMetadata(title="A Book", author="An Author", total_chapters=3),
        chapter=ChapterContent(index=0, title="Opening", content="Real content."),
        highlights=[Highlight(id="h-1", text="Real content.", paragraph_index=0)],
    )
    assert snapshot.external_identity == ExternalIdentity(
        source_system="reader", external_book_id="book-1"
    )
    assert snapshot.chapter.index == 0
    assert snapshot.highlights[0].id == "h-1"
    payload = snapshot.model_dump(mode="json")
    assert payload["source_system"] == "reader"
    assert "identity" not in payload


def test_snapshot_rejects_empty_content_and_private_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ChapterContent(index=0, title="Opening", content="")
    with pytest.raises(ValidationError):
        BookMetadata(title="A Book", moon_private=True)
```

- [ ] **Step 5: Run the book-contract tests and verify they fail**

Run:

```bash
uv run pytest tests/test_contract_books.py -v
```

Expected: FAIL because `contracts.books` does not exist.

- [ ] **Step 6: Implement strict book contracts**

Create `contracts/base.py` with the shared strict base:

```python
from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
```

Define these exact fields:

```python
class ExternalIdentity(ContractModel):
    source_system: str = Field(min_length=1, max_length=80)
    external_book_id: str = Field(min_length=1, max_length=240)


class BookMetadata(ContractModel):
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(default="", max_length=300)
    language: str = Field(default="", max_length=40)
    total_chapters: int = Field(default=0, ge=0)


class BookRef(ContractModel):
    book_id: str = Field(min_length=1, max_length=120)


class ChapterContent(ContractModel):
    index: int = Field(ge=0)
    title: str = Field(default="", max_length=500)
    content: str = Field(min_length=1)
    source_locator: str = Field(default="", max_length=500)


class Highlight(ContractModel):
    id: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1)
    note: str = ""
    paragraph_index: int | None = Field(default=None, ge=0)
    page: int | None = Field(default=None, ge=1)


class UserNote(ContractModel):
    id: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1)
    paragraph_index: int | None = Field(default=None, ge=0)


class Annotation(ContractModel):
    id: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1)
    author_label: str = Field(default="", max_length=120)
    kind: str = Field(default="annotation", max_length=80)
    paragraph_index: int | None = Field(default=None, ge=0)


class Reflection(ContractModel):
    id: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1)
    author_label: str = Field(default="", max_length=120)


class ChapterSnapshot(ContractModel):
    source_system: str = Field(min_length=1, max_length=80)
    external_book_id: str = Field(min_length=1, max_length=240)
    book: BookMetadata
    chapter: ChapterContent
    highlights: tuple[Highlight, ...] = ()
    user_notes: tuple[UserNote, ...] = ()
    annotations: tuple[Annotation, ...] = ()
    reflections: tuple[Reflection, ...] = ()

    @property
    def external_identity(self) -> ExternalIdentity:
        return ExternalIdentity(
            source_system=self.source_system,
            external_book_id=self.external_book_id,
        )
```

This keeps the serialized `ChapterSnapshot` shape identical to design section
4.2 while retaining `ExternalIdentity` as a reusable lookup key. Re-export
public names from `contracts/__init__.py`.

- [ ] **Step 7: Run focused and full quality checks**

Run:

```bash
uv run pytest tests/test_errors.py tests/test_contract_books.py -v
uv run ruff check .
uv run mypy src
git diff --check
```

Expected: all commands pass.

- [ ] **Step 8: Commit errors and book contracts**

```bash
git add src/cove_book_forge/errors.py src/cove_book_forge/contracts \
  tests/test_errors.py tests/test_contract_books.py
git commit -m "feat: add book contracts and errors"
```

---

### Task 3: Add Analysis, Plan, and Job Contracts

**Files:**
- Create: `src/cove_book_forge/contracts/analysis.py`
- Create: `src/cove_book_forge/contracts/jobs.py`
- Modify: `src/cove_book_forge/contracts/__init__.py`
- Create: `tests/test_contract_analysis.py`
- Create: `tests/test_contract_jobs.py`

**Interfaces:**
- Produces: `ChapterAnalysis` and its nested immutable item types.
- Produces: `ForgeTarget`, `ForgeJobStatus`, `ForgeJobControl`, `CostEstimate`, `ForgePlan`, `ForgeJob`, and `ForgeAccepted`.
- Later provider, analyzer, job, output, and MCP plans consume these exact contracts.

- [ ] **Step 1: Write failing analysis-schema tests**

Create `tests/test_contract_analysis.py`:

```python
import pytest
from pydantic import ValidationError

from cove_book_forge.contracts.analysis import ChapterAnalysis, Framework


def test_analysis_requires_actionable_core_content() -> None:
    analysis = ChapterAnalysis(
        core_idea="Choose the smallest reversible action.",
        frameworks=[
            Framework(
                name="Reversible step",
                when_to_use="When uncertainty is high.",
                how=("Reduce scope.", "Observe the result."),
                why="It limits downside.",
            )
        ],
        key_takeaways=("Prefer reversible commitments.",),
    )
    assert analysis.frameworks[0].how == ("Reduce scope.", "Observe the result.")


def test_analysis_rejects_an_empty_core_idea() -> None:
    with pytest.raises(ValidationError):
        ChapterAnalysis(core_idea="")
```

- [ ] **Step 2: Run the analysis test and verify it fails**

Run:

```bash
uv run pytest tests/test_contract_analysis.py -v
```

Expected: FAIL because `contracts.analysis` does not exist.

- [ ] **Step 3: Implement immutable analysis contracts**

Implement these immutable Pydantic contracts using `ContractModel`:

```python
from pydantic import Field

from cove_book_forge.contracts.base import ContractModel


class EvidenceRef(ContractModel):
    locator: str = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=1000)


class Framework(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    when_to_use: str = Field(default="", max_length=2000)
    how: tuple[str, ...] = ()
    why: str = Field(default="", max_length=2000)
    limitations: tuple[str, ...] = ()


class Concept(ContractModel):
    term: str = Field(min_length=1, max_length=200)
    definition: str = Field(min_length=1, max_length=4000)
    evidence_refs: tuple[EvidenceRef, ...] = ()


class MentalModel(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    explanation: str = Field(min_length=1, max_length=4000)
    when_to_use: str = Field(default="", max_length=2000)


class Method(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    steps: tuple[str, ...] = ()
    when_to_use: str = Field(default="", max_length=2000)
    limitations: tuple[str, ...] = ()


class AntiPattern(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    why: str = Field(min_length=1, max_length=2000)
    alternative: str = Field(default="", max_length=2000)


class DecisionRule(ContractModel):
    rule: str = Field(min_length=1, max_length=2000)
    conditions: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()


class WorkedExample(ContractModel):
    title: str = Field(min_length=1, max_length=200)
    situation: str = Field(default="", max_length=3000)
    application: str = Field(default="", max_length=3000)
    result: str = Field(default="", max_length=3000)


class QualityWarning(ContractModel):
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=1, max_length=1200)


class ChapterAnalysis(ContractModel):
    core_idea: str = Field(min_length=1, max_length=4000)
    frameworks: tuple[Framework, ...] = ()
    concepts: tuple[Concept, ...] = ()
    mental_models: tuple[MentalModel, ...] = ()
    methods: tuple[Method, ...] = ()
    anti_patterns: tuple[AntiPattern, ...] = ()
    decision_rules: tuple[DecisionRule, ...] = ()
    worked_examples: tuple[WorkedExample, ...] = ()
    key_takeaways: tuple[str, ...] = ()
    highlight_insights: tuple[str, ...] = ()
    annotation_insights: tuple[str, ...] = ()
    topic_tags: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    quality_warnings: tuple[QualityWarning, ...] = ()
```

Do not truncate overlong model output silently; Pydantic validation must reject it so the later analyzer can perform a bounded repair.

- [ ] **Step 4: Write failing plan and job tests**

Create `tests/test_contract_jobs.py`:

```python
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cove_book_forge.contracts.jobs import (
    CostEstimate,
    ForgeJob,
    ForgeJobStatus,
    ForgePlan,
    ForgeTarget,
)


def test_forge_plan_records_preflight_scope_and_expiry() -> None:
    now = datetime.now(UTC)
    plan = ForgePlan(
        plan_id="plan-1",
        book_id="book-1",
        book_fingerprint="sha256:abc",
        target=ForgeTarget.SKILL,
        total_chapters=12,
        processed_chapters=3,
        pending_chapters=(3, 4, 5, 6, 7, 8, 9, 10, 11),
        provider="openai-compatible",
        model="deepseek-v4-flash",
        generator_version="0.1",
        prompt_version="chapter-v1",
        estimate=CostEstimate(input_tokens=380_000, model_calls=12),
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    assert plan.remaining_chapters == 9
    assert plan.is_expired(now) is False

    invalid_scope = plan.model_dump()
    invalid_scope["processed_chapters"] = 13
    with pytest.raises(ValidationError):
        ForgePlan.model_validate(invalid_scope)


def test_cost_estimate_requires_a_complete_ordered_money_range() -> None:
    with pytest.raises(ValidationError):
        CostEstimate(currency="USD", minimum=Decimal("1.00"))
    with pytest.raises(ValidationError):
        CostEstimate(
            currency="USD",
            minimum=Decimal("2.00"),
            maximum=Decimal("1.00"),
        )


def test_job_exposes_progress_without_source_text() -> None:
    job = ForgeJob(
        job_id="job-1",
        book_id="book-1",
        target=ForgeTarget.SKILL,
        status=ForgeJobStatus.ANALYZING,
        processed_chapters=4,
        total_chapters=12,
    )
    assert job.progress == 4 / 12
    assert "content" not in job.model_dump()

    with pytest.raises(ValidationError):
        ForgeJob(
            job_id="job-invalid",
            book_id="book-1",
            target=ForgeTarget.SKILL,
            processed_chapters=13,
            total_chapters=12,
        )
```

- [ ] **Step 5: Run the plan/job tests and verify they fail**

Run:

```bash
uv run pytest tests/test_contract_jobs.py -v
```

Expected: FAIL because `contracts.jobs` does not exist.

- [ ] **Step 6: Implement plan and job contracts**

Use these enums:

```python
class ForgeTarget(StrEnum):
    OBSIDIAN = "obsidian"
    SKILL = "skill"


class ForgeJobStatus(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    PARSING = "parsing"
    ANALYZING = "analyzing"
    SYNTHESIZING = "synthesizing"
    VALIDATING = "validating"
    PUBLISHING = "publishing"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ForgeJobControl(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"


class CostEstimate(ContractModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    minimum: Decimal | None = Field(default=None, ge=0)
    maximum: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_complete_ordered_money_range(self) -> Self:
        values = (self.currency, self.minimum, self.maximum)
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError("currency, minimum, and maximum must be supplied together")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum must not exceed maximum")
        return self

    @property
    def available(self) -> bool:
        return self.currency is not None and self.minimum is not None and self.maximum is not None


class ForgePlan(ContractModel):
    plan_id: str = Field(min_length=1, max_length=120)
    book_id: str = Field(min_length=1, max_length=120)
    book_fingerprint: str = Field(min_length=1, max_length=160)
    target: ForgeTarget
    total_chapters: int = Field(ge=0)
    processed_chapters: int = Field(ge=0)
    pending_chapters: tuple[int, ...] = ()
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=240)
    generator_version: str = Field(min_length=1, max_length=80)
    prompt_version: str = Field(min_length=1, max_length=80)
    estimate: CostEstimate
    created_at: datetime
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value

    @model_validator(mode="after")
    def require_consistent_scope(self) -> Self:
        if self.processed_chapters > self.total_chapters:
            raise ValueError("processed_chapters must not exceed total_chapters")
        if len(set(self.pending_chapters)) != len(self.pending_chapters):
            raise ValueError("pending_chapters must not contain duplicates")
        if any(index < 0 or index >= self.total_chapters for index in self.pending_chapters):
            raise ValueError("pending chapter index is outside the book")
        if self.processed_chapters + len(self.pending_chapters) != self.total_chapters:
            raise ValueError("processed and pending chapters must cover the book")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self

    @property
    def remaining_chapters(self) -> int:
        return len(self.pending_chapters)

    def is_expired(self, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return now >= self.expires_at


def _now_utc() -> datetime:
    return datetime.now(UTC)


class ForgeJob(ContractModel):
    job_id: str = Field(min_length=1, max_length=120)
    book_id: str = Field(min_length=1, max_length=120)
    target: ForgeTarget
    status: ForgeJobStatus = ForgeJobStatus.QUEUED
    processed_chapters: int = Field(default=0, ge=0)
    total_chapters: int = Field(default=0, ge=0)
    current_chapter: int | None = Field(default=None, ge=0)
    failed_chapters: tuple[int, ...] = ()
    error: ForgeErrorDetail | None = None
    created_at: datetime = Field(default_factory=_now_utc)
    updated_at: datetime = Field(default_factory=_now_utc)

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value

    @model_validator(mode="after")
    def require_consistent_progress(self) -> Self:
        if self.processed_chapters > self.total_chapters:
            raise ValueError("processed_chapters must not exceed total_chapters")
        indexes = self.failed_chapters
        if self.current_chapter is not None:
            indexes = (*indexes, self.current_chapter)
        if any(index < 0 or index >= self.total_chapters for index in indexes):
            raise ValueError("job chapter index is outside the book")
        if len(set(self.failed_chapters)) != len(self.failed_chapters):
            raise ValueError("failed_chapters must not contain duplicates")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not be earlier than created_at")
        return self

    @property
    def progress(self) -> float:
        if self.total_chapters == 0:
            return 0.0
        return min(1.0, max(0.0, self.processed_chapters / self.total_chapters))


class ForgeAccepted(ContractModel):
    accepted: Literal[True] = True
    job_id: str = Field(min_length=1, max_length=120)
    status: ForgeJobStatus
    target: ForgeTarget
```

Import `Decimal`, `Literal`, `Self`, `UTC`, `datetime`, `field_validator`,
`model_validator`, `ForgeErrorDetail`, and `ContractModel` explicitly.
Re-export all public job types from `contracts/__init__.py`.

- [ ] **Step 7: Run contract tests and quality checks**

Run:

```bash
uv run pytest tests/test_contract_analysis.py tests/test_contract_jobs.py -v
uv run ruff check .
uv run mypy src
git diff --check
```

Expected: all commands pass.

- [ ] **Step 8: Commit analysis and job contracts**

```bash
git add src/cove_book_forge/contracts tests/test_contract_analysis.py \
  tests/test_contract_jobs.py
git commit -m "feat: define analysis and job contracts"
```

---

### Task 4: Implement Configuration Loading Without Stored Secrets

**Files:**
- Create: `src/cove_book_forge/config/__init__.py`
- Create: `src/cove_book_forge/config/models.py`
- Create: `src/cove_book_forge/config/loader.py`
- Create: `tests/test_config.py`

**Interfaces:**
- Produces: `AppConfig`, `LibraryConfig`, `ModelConfig`, `ObsidianOutputConfig`, `SkillOutputConfig`, `OutputsConfig`, and `FullBookForgeConfig`.
- Produces: `default_config_path() -> Path`, `load_config(path: Path | None = None) -> AppConfig`, and `dump_config(config: AppConfig) -> str`.
- Raises: `ForgeException(CONFIG_INVALID, ...)` for missing or invalid required configuration.

- [ ] **Step 1: Write failing configuration tests**

Create `tests/test_config.py`:

```python
from pathlib import Path

import pytest

from cove_book_forge.config.loader import dump_config, load_config
from cove_book_forge.config.models import AppConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException


def test_config_round_trip_stores_key_name_not_secret(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """
library:
  enabled: false
model:
  provider: openai-compatible
  base_url: https://api.deepseek.com
  model: deepseek-v4-flash
  api_key_env: DEEPSEEK_API_KEY
outputs:
  obsidian:
    enabled: false
  skills:
    enabled: false
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "actual-secret")
    config = load_config(path)
    rendered = dump_config(config)
    assert config.model.api_key_env == "DEEPSEEK_API_KEY"
    assert "DEEPSEEK_API_KEY" in rendered
    assert "actual-secret" not in rendered


def test_invalid_config_is_a_structured_public_error(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("model: {}", encoding="utf-8")
    with pytest.raises(ForgeException) as caught:
        load_config(path)
    assert caught.value.code is ForgeErrorCode.CONFIG_INVALID


def test_defaults_are_local_first_and_require_full_book_confirmation() -> None:
    config = AppConfig.model_validate(
        {"model": {"provider": "openai-compatible", "model": "local-model"}}
    )
    assert config.library.enabled is True
    assert config.full_book_forge.require_preflight_confirmation is True
    assert config.telemetry_enabled is False
```

- [ ] **Step 2: Run configuration tests and verify they fail**

Run:

```bash
uv run pytest tests/test_config.py -v
```

Expected: FAIL because the config package does not exist.

- [ ] **Step 3: Implement strict configuration models**

Implement strict, immutable Pydantic models:

```python
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LibraryConfig(ConfigModel):
    enabled: bool = True
    copy_imports: bool = True
    data_dir: Path | None = None

    @model_validator(mode="after")
    def require_absolute_custom_data_path(self) -> Self:
        if self.data_dir is not None and not self.data_dir.is_absolute():
            raise ValueError("custom library data_dir must be absolute")
        return self


class ModelConfig(ConfigModel):
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=240)
    base_url: HttpUrl | None = None
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    max_concurrency: int = Field(default=2, ge=1, le=16)
    requests_per_minute: int = Field(default=20, ge=1, le=10_000)


class ObsidianOutputConfig(ConfigModel):
    enabled: bool = False
    vault_path: Path | None = None
    notes_folder: str = Field(default="Books", min_length=1, max_length=120)
    cards_folder: str = Field(default="Cards", min_length=1, max_length=120)

    @model_validator(mode="after")
    def require_absolute_enabled_path(self) -> Self:
        if self.enabled and (self.vault_path is None or not self.vault_path.is_absolute()):
            raise ValueError("enabled Obsidian output requires an absolute vault_path")
        return self


class SkillOutputConfig(ConfigModel):
    enabled: bool = False
    canonical_path: Path | None = None
    install_to: tuple[Literal["agents", "codex", "claude"], ...] = ()

    @model_validator(mode="after")
    def require_absolute_enabled_path(self) -> Self:
        if self.enabled and (self.canonical_path is None or not self.canonical_path.is_absolute()):
            raise ValueError("enabled Skill output requires an absolute canonical_path")
        return self


class OutputsConfig(ConfigModel):
    obsidian: ObsidianOutputConfig = Field(default_factory=ObsidianOutputConfig)
    skills: SkillOutputConfig = Field(default_factory=SkillOutputConfig)


class FullBookForgeConfig(ConfigModel):
    require_preflight_confirmation: bool = True
    plan_ttl_minutes: int = Field(default=30, ge=1, le=1440)


class AppConfig(ConfigModel):
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    model: ModelConfig
    outputs: OutputsConfig = Field(default_factory=OutputsConfig)
    full_book_forge: FullBookForgeConfig = Field(default_factory=FullBookForgeConfig)
    telemetry_enabled: Literal[False] = False
```

Pydantic's `Literal` and validation bounds enforce provider/model presence, concurrency, request rate, plan TTL, install-target values, absolute enabled output paths, and the v0.1 telemetry prohibition.

- [ ] **Step 4: Implement YAML loading and redaction-safe dumping**

Implement the loader with these signatures and behavior:

```python
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_path, user_data_path
from pydantic import ValidationError

from cove_book_forge.config.models import AppConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException


def default_config_path() -> Path:
    return user_config_path("cove-book-forge-mcp") / "config.yaml"


def default_data_path() -> Path:
    return user_data_path("cove-book-forge-mcp")


def library_data_path(config: AppConfig) -> Path:
    return config.library.data_dir or default_data_path()


def load_config(path: Path | None = None) -> AppConfig:
    source = (path or default_config_path()).expanduser()
    try:
        raw: Any = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("configuration root must be a mapping")
        return AppConfig.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as exc:
        raise ForgeException(
            ForgeErrorCode.CONFIG_INVALID,
            "Configuration is invalid.",
            details={"path": str(source)},
            cause=exc,
        ) from exc


def dump_config(config: AppConfig) -> str:
    payload = config.model_dump(mode="json")
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
```

Export the models and four loader functions from `config/__init__.py`. No model contains a secret-value field, so dumping cannot serialize an API key.

- [ ] **Step 5: Run configuration and global checks**

Run:

```bash
uv run pytest tests/test_config.py -v
uv run pytest -q
uv run ruff check .
uv run mypy src
git diff --check
```

Expected: all commands pass.

- [ ] **Step 6: Commit configuration**

```bash
git add src/cove_book_forge/config tests/test_config.py
git commit -m "feat: add local-first configuration"
```

---

### Task 5: Enforce Authorized Output Paths

**Files:**
- Create: `src/cove_book_forge/config/paths.py`
- Modify: `src/cove_book_forge/config/__init__.py`
- Create: `tests/test_paths.py`

**Interfaces:**
- Produces: `AuthorizedPathPolicy(roots: tuple[Path, ...])`.
- Produces: `validate_root(path: Path) -> Path` and `resolve_target(root: Path, *parts: str) -> Path`.
- Raises: `ForgeException(PATH_NOT_ALLOWED, ...)` for broad roots, traversal, invalid components, or symlink escape.

- [ ] **Step 1: Write failing path-policy tests**

Create `tests/test_paths.py`:

```python
from pathlib import Path

import pytest

from cove_book_forge.config.paths import AuthorizedPathPolicy
from cove_book_forge.errors import ForgeErrorCode, ForgeException


def test_resolve_target_stays_inside_authorized_root(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    policy = AuthorizedPathPolicy((root,))
    assert policy.resolve_target(root, "Books", "Safe.md") == root / "Books" / "Safe.md"


def test_resolve_target_rejects_traversal(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    policy = AuthorizedPathPolicy((root,))
    with pytest.raises(ForgeException) as caught:
        policy.resolve_target(root, "..", "outside.md")
    assert caught.value.code is ForgeErrorCode.PATH_NOT_ALLOWED


def test_resolve_target_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    policy = AuthorizedPathPolicy((root,))
    with pytest.raises(ForgeException):
        policy.resolve_target(root, "escape", "file.md")


@pytest.mark.parametrize("broad_root", [Path(Path.cwd().anchor), Path.home()])
def test_policy_rejects_filesystem_and_home_roots(broad_root: Path) -> None:
    with pytest.raises(ForgeException) as caught:
        AuthorizedPathPolicy((broad_root,))
    assert caught.value.code is ForgeErrorCode.PATH_NOT_ALLOWED
```

- [ ] **Step 2: Run path tests and verify they fail**

Run:

```bash
uv run pytest tests/test_paths.py -v
```

Expected: FAIL because `config.paths` does not exist.

- [ ] **Step 3: Implement the realpath policy**

Implement the policy as a frozen dataclass:

```python
from dataclasses import dataclass
from pathlib import Path

from cove_book_forge.errors import ForgeErrorCode, ForgeException


def _is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _path_error(message: str, path: Path) -> ForgeException:
    return ForgeException(
        ForgeErrorCode.PATH_NOT_ALLOWED,
        message,
        details={"path": str(path)},
    )


@dataclass(frozen=True)
class AuthorizedPathPolicy:
    roots: tuple[Path, ...]

    def __post_init__(self) -> None:
        normalized = tuple(self.validate_root(root) for root in self.roots)
        if not normalized:
            raise _path_error("At least one authorized root is required.", Path("."))
        object.__setattr__(self, "roots", normalized)

    @staticmethod
    def validate_root(path: Path) -> Path:
        try:
            root = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise _path_error("Authorized root does not exist.", path) from exc
        if not root.is_dir():
            raise _path_error("Authorized root must be a directory.", root)
        if root == Path(root.anchor) or root == Path.home().resolve():
            raise _path_error("Authorized root is too broad.", root)
        return root

    def resolve_target(self, root: Path, *parts: str) -> Path:
        normalized_root = self.validate_root(root)
        if normalized_root not in self.roots:
            raise _path_error("Root was not explicitly authorized.", normalized_root)

        current = normalized_root
        for part in parts:
            if (
                not part
                or part in {".", ".."}
                or "\x00" in part
                or "/" in part
                or "\\" in part
                or Path(part).is_absolute()
            ):
                raise _path_error("Target contains an invalid path component.", current / part)
            candidate = current / part
            if candidate.exists() or candidate.is_symlink():
                try:
                    candidate = candidate.resolve(strict=True)
                except OSError as exc:
                    raise _path_error("Target path cannot be resolved.", candidate) from exc
                if not _is_within(candidate, normalized_root):
                    raise _path_error("Target escapes its authorized root.", candidate)
            current = candidate

        if not _is_within(current, normalized_root):
            raise _path_error("Target escapes its authorized root.", current)
        return current
```

This code is intentionally read-only. Later output code must re-check containment immediately before atomic replacement to reduce time-of-check/time-of-use risk.

- [ ] **Step 4: Run path tests and all quality checks**

Run:

```bash
uv run pytest tests/test_paths.py -v
uv run pytest -q
uv run ruff check .
uv run mypy src
git diff --check
```

Expected: all commands pass.

- [ ] **Step 5: Commit authorized-path validation**

```bash
git add src/cove_book_forge/config tests/test_paths.py
git commit -m "feat: enforce authorized output paths"
```

---

### Task 6: Add the Read-Only Doctor CLI

**Files:**
- Create: `src/cove_book_forge/doctor.py`
- Create: `src/cove_book_forge/cli.py`
- Create: `src/cove_book_forge/__main__.py`
- Create: `tests/test_cli_doctor.py`
- Modify: `README.md`

**Interfaces:**
- Produces: `CheckStatus`, `DoctorCheck`, `DoctorReport`, and `run_doctor(config_path: Path | None = None) -> DoctorReport`.
- Produces: `cove-book-forge doctor --config PATH --json`.
- Doctor is strictly read-only in this phase: it does not create directories, contact a Provider, or modify configuration.

- [ ] **Step 1: Write failing doctor CLI tests**

Create `tests/test_cli_doctor.py`:

```python
import json
from pathlib import Path

from typer.testing import CliRunner

from cove_book_forge.cli import app


runner = CliRunner()


def test_doctor_reports_valid_local_configuration(tmp_path: Path, monkeypatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text(
        f"""
library:
  enabled: true
  data_dir: {data}
model:
  provider: openai-compatible
  model: local-model
outputs:
  obsidian:
    enabled: false
  skills:
    enabled: false
""".strip(),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["checks"][0]["name"] == "configuration"


def test_doctor_reports_missing_key_environment_without_printing_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
model:
  provider: openai-compatible
  model: cloud-model
  api_key_env: MISSING_TEST_KEY
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.delenv("MISSING_TEST_KEY", raising=False)
    result = runner.invoke(app, ["doctor", "--config", str(config), "--json"])
    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert payload["ok"] is False
    assert "MISSING_TEST_KEY" in result.stdout
    assert "Authorization" not in result.stdout
```

- [ ] **Step 2: Run doctor tests and verify they fail**

Run:

```bash
uv run pytest tests/test_cli_doctor.py -v
```

Expected: FAIL because `cove_book_forge.cli` does not exist.

- [ ] **Step 3: Implement typed doctor results**

`run_doctor()` checks, in order:

1. configuration loads;
2. configured API-key environment variable is present when specified;
3. enabled library data directory exists, is a directory, and is writable without creating a probe file;
4. enabled Obsidian root exists and passes `AuthorizedPathPolicy.validate_root`;
5. enabled Skill canonical root exists and passes the same policy;

Configuration loading in check 1 also validates that every Skill install
target is recognized; invalid values produce the configuration failure rather
than a partially valid report.

Provider network health, parser dependencies, database migrations, and installation conflicts are added by later plans under new checks without changing these names.

Implement the report and read-only checks with this structure:

```python
import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cove_book_forge.config import AppConfig, library_data_path, load_config
from cove_book_forge.config.paths import AuthorizedPathPolicy
from cove_book_forge.errors import ForgeException


class CheckStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class DoctorCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1, max_length=120)
    status: CheckStatus
    message: str = Field(min_length=1, max_length=1200)


class DoctorReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.status is not CheckStatus.FAIL for check in self.checks)


def _directory_check(name: str, path: Path) -> DoctorCheck:
    if not path.exists() or not path.is_dir():
        return DoctorCheck(name=name, status=CheckStatus.FAIL, message=f"Missing: {path}")
    if not os.access(path, os.W_OK):
        return DoctorCheck(name=name, status=CheckStatus.FAIL, message=f"Not writable: {path}")
    try:
        AuthorizedPathPolicy((path,))
    except ForgeException as exc:
        return DoctorCheck(name=name, status=CheckStatus.FAIL, message=str(exc))
    return DoctorCheck(name=name, status=CheckStatus.PASS, message=f"Ready: {path}")


def _checks_for_config(config: AppConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = [
        DoctorCheck(
            name="configuration",
            status=CheckStatus.PASS,
            message="Configuration loaded.",
        )
    ]
    key_name = config.model.api_key_env
    if key_name:
        status = CheckStatus.PASS if os.environ.get(key_name) else CheckStatus.FAIL
        message = f"Environment variable is {'set' if status is CheckStatus.PASS else 'missing'}: {key_name}"
        checks.append(DoctorCheck(name="model_api_key", status=status, message=message))
    if config.library.enabled:
        checks.append(_directory_check("library_data", library_data_path(config)))
    if config.outputs.obsidian.enabled and config.outputs.obsidian.vault_path is not None:
        checks.append(_directory_check("obsidian_vault", config.outputs.obsidian.vault_path))
    if config.outputs.skills.enabled and config.outputs.skills.canonical_path is not None:
        checks.append(_directory_check("skill_root", config.outputs.skills.canonical_path))
    return checks


def run_doctor(config_path: Path | None = None) -> DoctorReport:
    try:
        config = load_config(config_path)
    except ForgeException as exc:
        return DoctorReport(
            checks=(DoctorCheck(name="configuration", status=CheckStatus.FAIL, message=str(exc)),)
        )
    return DoctorReport(checks=tuple(_checks_for_config(config)))
```

Do not include environment-variable values in a check message.

- [ ] **Step 4: Implement Typer commands and JSON output**

Create a Typer application with this command skeleton:

```python
import json
from pathlib import Path
from typing import Annotated

import typer

from cove_book_forge.doctor import run_doctor


app = typer.Typer(no_args_is_help=True, help="Forge books into reusable knowledge.")


@app.command()
def doctor(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    report = run_doctor(config)
    if json_output:
        payload = {
            "ok": report.ok,
            "checks": [item.model_dump(mode="json") for item in report.checks],
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        for check in report.checks:
            typer.echo(f"{check.status.value.upper():4} {check.name}: {check.message}")
    raise typer.Exit(code=0 if report.ok else 1)
```

Create `__main__.py`:

```python
from cove_book_forge.cli import app


if __name__ == "__main__":
    app()
```

`--config` accepts an existing file. Human output uses one line per check without Rich markup in JSON mode.

Exit codes:

```text
0: no FAIL checks
1: one or more FAIL checks
2: CLI usage error
```

`python -m cove_book_forge` runs the same Typer app.

- [ ] **Step 5: Add README installation and doctor usage**

Document:

```bash
uv sync --group dev
uv run cove-book-forge doctor --config /absolute/path/config.yaml
```

State that this foundation command is read-only and that parser/provider/MCP checks arrive in their corresponding implementation phases.

- [ ] **Step 6: Run CLI and global quality checks**

Run:

```bash
uv run pytest tests/test_cli_doctor.py -v
uv run pytest -q
uv run ruff check .
uv run mypy src
uv build
git diff --check
```

Expected: all commands pass and both wheel and source distribution are built.

- [ ] **Step 7: Commit the doctor CLI**

```bash
git add src/cove_book_forge/doctor.py src/cove_book_forge/cli.py \
  src/cove_book_forge/__main__.py tests/test_cli_doctor.py README.md
git commit -m "feat: add read-only doctor command"
```

---

### Task 7: Add Continuous Integration and Foundation Completion Checks

**Files:**
- Create: `.github/workflows/ci.yml`
- Modify: `README.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Produces: CI validation for Python 3.11, 3.12, 3.13, and 3.14.
- Produces: one canonical local verification command sequence.

- [ ] **Step 1: Add the CI workflow**

Create `.github/workflows/ci.yml` with:

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12", "3.13", "3.14"]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          python-version: ${{ matrix.python-version }}
          enable-cache: true
      - run: uv sync --locked --group dev
      - run: uv run pytest --cov=cove_book_forge --cov-report=term-missing
      - run: uv run ruff check .
      - run: uv run mypy src
      - run: uv build
```

- [ ] **Step 2: Document contributor verification and update changelog**

Add to README:

```bash
uv sync --group dev
uv run pytest --cov=cove_book_forge --cov-report=term-missing
uv run ruff check .
uv run mypy src
uv build
```

Update the `Unreleased` section to list the installable foundation, contracts, configuration, path policy, doctor command, and Python support matrix.

- [ ] **Step 3: Run the complete foundation verification**

Run:

```bash
uv lock --check
uv run pytest --cov=cove_book_forge --cov-report=term-missing
uv run ruff check .
uv run mypy src
uv build
git diff --check
git status --short
```

Expected: every command passes; `git status --short` lists only the CI/README/CHANGELOG changes intended for this task.

- [ ] **Step 4: Commit CI and foundation completion**

```bash
git add .github/workflows/ci.yml README.md CHANGELOG.md uv.lock
git commit -m "ci: add foundation quality gates"
```

- [ ] **Step 5: Verify the plan's completion condition**

Run:

```bash
git status --short --branch
git log --oneline --decorate -8
```

Expected: clean worktree on `codex/initial-design`, with seven focused implementation commits after the design and plan commits. The package installs, `doctor` runs read-only, and all public foundation types are importable.

## Foundation Exit Criteria

This plan is complete only when:

- `uv build` produces a wheel and source distribution;
- tests pass on the active Python interpreter;
- Ruff and strict mypy pass;
- attribution tests protect the upstream credit and license notice;
- configuration cannot serialize an API key value;
- authorized path tests reject traversal and symlink escape;
- `cove-book-forge doctor --json` returns deterministic structured checks;
- the worktree is clean and all seven task commits exist.

After this plan, write and execute the next independent plan for secure ingestion and the optional managed library. That plan consumes `BookMetadata`, `BookRef`, `ChapterSnapshot`, `AppConfig`, `AuthorizedPathPolicy`, and `ForgeException` exactly as defined here.
