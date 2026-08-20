# Agent Skill Output and Installation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn one cached `AnalyzedChapter` into a deterministic, incrementally growing book Skill, publish it atomically in one canonical directory, and install it safely for Codex, Claude Code, or a generic Agent without another model call.

**Architecture:** A pure renderer builds a compact `SKILL.md` plus progressively loaded book references from the shared chapter analysis. A checksummed manifest locks book identity, slug, chapter fingerprints, generated paths, and managed hashes. A guarded canonical publisher stages a complete generation and atomically activates it; a separate installer creates verified managed symlinks where available and guarded managed copies otherwise.

**Tech Stack:** Python 3.11+, Pydantic 2, stdlib `hashlib`/`json`/`os`/`pathlib`/`shutil`/`stat`/`unicodedata`, pytest, Ruff, strict mypy.

**Spec:** `docs/superpowers/specs/2026-08-14-cove-book-forge-mcp-design.md` (§5, §9.2, §12, §14–17, §21 phase 8)

## Global Constraints

- Rendering consumes an existing `AnalyzedChapter`; it never calls a Provider, repairs model output, or reads the original book body.
- The canonical root comes only from enabled `SkillOutputConfig`. Installation targets come only from the explicitly selected `install_to` values and their documented conventional roots.
- A generated Skill contains Markdown, JSON, and minimal `agents/openai.yaml` only. It never contains scripts, executables, hooks, MCP configuration, `allowed-tools`, secrets, absolute source paths, raw prompts, or raw book text.
- `SKILL.md` frontmatter contains exactly `name` and `description`. The name is lowercase hyphen-case, matches the stable folder slug, and is at most 63 characters.
- `SKILL.md` stays concise and routes detailed content to one-level references. Generated book material is explicitly treated as untrusted reference content, never as agent instructions.
- Reject absolute/traversal/control/backslash paths, symlink escapes, broad/root output locations, external modification, non-managed targets, malformed manifests, and source-selected paths.
- Publishing and installation never overwrite an existing non-managed file, directory, or link. Failure leaves the last successful canonical Skill and installations usable.
- Re-publishing an unchanged fingerprint performs zero rewrites. Updating one chapter preserves other managed chapters and updates only affected aggregate files.
- The generated manifest stores controlled identity, fingerprints, relative paths, hashes, generator/schema versions, and timestamps; it stores no private note bodies, source text, credentials, or absolute paths.
- This phase implements single-chapter Skill output and installation only. Whole-book planning/jobs, MCP tools/transports, UI, and Cove private adapters remain later phases.

## Locked Public Interfaces

```python
class SkillInstallResult(ContractModel):
    target: Literal["agents", "codex", "claude"]
    path: str
    strategy: Literal["symlink", "copy"]
    unchanged: bool = False


class SkillPublishResult(ContractModel):
    book_key: str
    skill_slug: str
    canonical_path: str
    chapter_path: str
    input_fingerprint: str
    changed_paths: tuple[str, ...] = ()
    installations: tuple[SkillInstallResult, ...] = ()
    unchanged: bool = False


class AgentSkillOutput:
    def __init__(self, config: SkillOutputConfig) -> None: ...

    def publish(
        self,
        snapshot: ChapterSnapshot,
        analyzed: AnalyzedChapter,
    ) -> SkillPublishResult: ...
```

Internal pure boundary:

```python
class RenderedAgentSkill:
    files: Mapping[str, bytes]
    manifest: AgentSkillManifest
    skill_slug: str
    chapter_path: str


class AgentSkillRenderer:
    def render(
        self,
        snapshot: ChapterSnapshot,
        analyzed: AnalyzedChapter,
        previous: AgentSkillManifest | None,
    ) -> RenderedAgentSkill: ...
```

## Locked Managed Format

- `book_key = sha256(canonical JSON [source_system, external_book_id])[:16]`, shared with Obsidian output.
- First publication locks `skill_slug = <safe-title-prefix>--<book_key>`; later title changes update display text but never rename the Skill directory.
- Canonical visible entry: `<canonical_path>/<skill_slug>/`.
- Required files:

```text
<skill-slug>/
├── SKILL.md
├── agents/openai.yaml
├── chapters/ch0001-<safe-title>.md
├── glossary.md
├── patterns.md
├── cheatsheet.md
└── .cove-book-forge.json
```

- `SKILL.md` frontmatter is exactly `name` and `description`; its body contains book identity, coverage, a short usage workflow, safety instruction, core frameworks, and links to `glossary.md`, `patterns.md`, `cheatsheet.md`, and relevant `chapters/*.md`.
- `agents/openai.yaml` contains only quoted `interface.display_name`, `interface.short_description`, and `interface.default_prompt`; the prompt explicitly names `$<skill-slug>`.
- Chapter references contain the analyzed core idea, frameworks, concepts, mental models, methods, decision rules, anti-patterns, worked examples, takeaways, application cues, and source locators. They do not reproduce raw chapter content.
- `glossary.md`, `patterns.md`, and `cheatsheet.md` are deterministic book-level aggregates rebuilt from manifest summaries and current analysis.
- `.cove-book-forge.json` is canonical schema v1 with a checksum over the object excluding `checksum`; it records the complete managed file set and SHA-256 for conflict detection.

---

### Task 1: Render a concise deterministic Agent Skill

**Files:**

- Modify: `src/cove_book_forge/config/models.py`
- Modify: `src/cove_book_forge/contracts/outputs.py`
- Modify: `src/cove_book_forge/contracts/__init__.py`
- Modify: `src/cove_book_forge/outputs/__init__.py`
- Create: `src/cove_book_forge/outputs/skill_models.py`
- Create: `src/cove_book_forge/outputs/skill_render.py`
- Create: `tests/test_skill_render.py`
- Modify: `tests/test_config.py`

**Interfaces:**

- Consumes: `ChapterSnapshot`, `AnalyzedChapter`, `SkillOutputConfig`, and the existing canonical book-key/path-safety helpers.
- Produces: strict result/manifest models and pure `AgentSkillRenderer.render(...) -> RenderedAgentSkill`.

- [ ] **Step 1: Write RED contract/config tests.** Cover strict/frozen public results; enabled absolute canonical root; duplicate/unsupported install targets; stable slug syntax/length; title-change slug lock; path validation; manifest schema/checksum/hash/path/count bounds.
- [ ] **Step 2: Write RED golden renderer tests.** Assert the exact required tree, exact two-field `SKILL.md` frontmatter, concise progressive-disclosure body, three-field `agents/openai.yaml`, deterministic bytes, current chapter completeness, aggregate glossary/patterns/cheatsheet, multi-chapter preservation, and empty optional collections.
- [ ] **Step 3: Write RED safety tests.** Treat prompt injection, YAML delimiters, Markdown/HTML, control characters, links, absolute paths, and secret-like strings as inert reference text. Assert no scripts/hooks/executables/`allowed-tools`/MCP dependencies and no link escapes.
- [ ] **Step 4: Run RED.**

```bash
uv run --no-sync pytest tests/test_skill_render.py tests/test_config.py -v
```

- [ ] **Step 5: Implement the minimal pure renderer.** Normalize NFC/newlines; sanitize and bound the slug/filenames; quote YAML scalars; keep `SKILL.md` under 500 lines; route details one level deep; canonicalize manifest JSON and file hashes; never touch the filesystem.
- [ ] **Step 6: Run gates and commit.**

```bash
uv run --no-sync pytest tests/test_skill_render.py tests/test_config.py tests/test_contract_analysis.py -v
uv run --no-sync ruff check src/cove_book_forge/outputs src/cove_book_forge/contracts src/cove_book_forge/config tests/test_skill_render.py tests/test_config.py
uv run --no-sync mypy src/cove_book_forge
git diff --check
```

Commit: `feat: render managed book skills`

### Task 2: Validate and atomically publish the canonical Skill

**Files:**

- Create: `src/cove_book_forge/outputs/skill_managed.py`
- Create: `src/cove_book_forge/outputs/skill_publisher.py`
- Modify: `src/cove_book_forge/outputs/skill_models.py`
- Create: `tests/test_skill_managed.py`
- Create: `tests/test_skill_publisher.py`

**Interfaces:**

- Consumes: Task 1 rendered files and manifest.
- Produces: strict managed parsing/update planning and `CanonicalSkillPublisher.publish(render) -> SkillPublisherReceipt`.

- [ ] **Step 1: Write RED managed-validation tests.** Cover exact manifest checksum/canonical JSON, complete file-hash agreement, expected tree/file kinds, missing/extra/duplicate paths, bad SKILL/openai YAML structure, forbidden extensions/content, path traversal, symlinks, altered managed files, and safe fixed errors.
- [ ] **Step 2: Write RED update tests.** First publication, unchanged no-op, one-chapter incremental update, title-change path lock, historical chapter preservation, stale current files, occupied non-managed slug, missing/tampered managed files, another book identity, and concurrent precondition changes.
- [ ] **Step 3: Write RED atomicity/recovery tests.** Inject failure before/during stage, activation, manifest switch, cleanup, and process restart. Assert the last complete Skill remains discoverable, no competitor is overwritten/adopted/deleted, and owned debris is safely recoverable.
- [ ] **Step 4: Run RED.**

```bash
uv run --no-sync pytest tests/test_skill_managed.py tests/test_skill_publisher.py -v
```

- [ ] **Step 5: Implement guarded canonical publication.** Anchor the configured root with no-follow identity checks; bound reads/files/bytes; stage and validate a complete same-filesystem generation; activate with a managed atomic indirection or equivalent no-clobber transaction; verify the previous managed generation before replacement; fsync durability boundaries; recover only identity-proven owned transactions. Reuse existing path/error conventions without modifying Obsidian behavior.
- [ ] **Step 6: Run gates and commit.**

```bash
uv run --no-sync pytest tests/test_skill_managed.py tests/test_skill_publisher.py tests/test_skill_render.py -W error::ResourceWarning -v
uv run --no-sync ruff check src/cove_book_forge/outputs tests/test_skill_managed.py tests/test_skill_publisher.py
uv run --no-sync mypy src/cove_book_forge
git diff --check
```

Commit: `feat: publish canonical book skills`

### Task 3: Install safely and expose `AgentSkillOutput`

**Files:**

- Create: `src/cove_book_forge/outputs/skill_install.py`
- Create: `src/cove_book_forge/outputs/agent_skill.py`
- Modify: `src/cove_book_forge/outputs/__init__.py`
- Modify: `src/cove_book_forge/contracts/__init__.py`
- Create: `tests/test_skill_install.py`
- Create: `tests/test_agent_skill_output.py`
- Create: `tests/test_skill_integration.py`

**Interfaces:**

- Consumes: Tasks 1–2 canonical Skill and `SkillOutputConfig.install_to`.
- Produces: `AgentSkillOutput.publish(...) -> SkillPublishResult` with per-target installation results.

- [ ] **Step 1: Write RED installer tests.** Resolve selected conventional roots only (`agents`, `codex`, `claude`); prefer a relative symlink to the canonical managed Skill; fall back to a staged verified managed copy only when symlinks are unsupported; validate all roots no-follow and read/write/execute; reject broad roots and unsafe environment overrides.
- [ ] **Step 2: Write RED conflict/idempotence tests.** Existing non-managed file/directory/link, wrong managed link target, modified managed copy, target replacement races, duplicate targets, partial copy failure, retry, unchanged installation, and safe `INSTALL_CONFLICT`/path-free errors.
- [ ] **Step 3: Write RED service/integration tests.** Analyze once with a fake Provider and persistent SQLite cache; publish Obsidian then Skill and Skill then Obsidian with one total Provider call; restart and republish with zero calls/rewrites; change one note and prove one re-analysis plus one managed chapter/aggregate/installation update.
- [ ] **Step 4: Run RED.**

```bash
uv run --no-sync pytest tests/test_skill_install.py tests/test_agent_skill_output.py tests/test_skill_integration.py -v
```

- [ ] **Step 5: Implement installer and public service.** Publish canonical content first, install selected targets second, return only safe relative/display paths, and preserve canonical success if an installation reports `INSTALL_CONFLICT`. Never call a Provider. Keep symlink/copy ownership metadata sufficient for future verification and removal.
- [ ] **Step 6: Validate a generated fixture as a real Skill.** Run the repository scanner and, when available, the installed `skill-creator/scripts/quick_validate.py` against a temporary generated Skill. Forward-test one fresh agent using only the generated artifact and a realistic application question; confirm it discovers the right reference without treating book text as instructions.
- [ ] **Step 7: Run gates and commit.**

```bash
uv run --no-sync pytest tests/test_skill_install.py tests/test_agent_skill_output.py tests/test_skill_integration.py tests/test_obsidian_integration.py -W error::ResourceWarning -v
uv run --no-sync ruff check src/cove_book_forge/outputs tests/test_skill_install.py tests/test_agent_skill_output.py tests/test_skill_integration.py
uv run --no-sync mypy src/cove_book_forge
git diff --check
```

Commit: `feat: install generated book skills`

### Task 4: Add Doctor checks, docs, and complete phase verification

**Files:**

- Modify: `src/cove_book_forge/doctor.py`
- Modify: `tests/test_cli_doctor.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Create: `examples/publish_chapter_skill.py`
- Create: `tests/test_skill_docs.py`

**Interfaces:**

- Consumes: Tasks 1–3.
- Produces: documented single-chapter “炼成 Skill” boundary ready for later whole-book jobs and MCP tools.

- [ ] **Step 1: Write Doctor RED tests.** Disabled Skill output is a non-blocking warning. Enabled output validates canonical and selected installation roots read-only with anchored effective-access checks; missing/unsafe/symlink/broad/unwritable roots fail with fixed path-free messages and zero created files.
- [ ] **Step 2: Write docs/example RED tests.** Execute and strict-type-check the public example; assert README output tree, cache reuse, invocation syntax, Codex/Claude/generic install behavior, conflict behavior, and truthful boundary that whole-book jobs/MCP are still future. Preserve the exact `book-to-skill`/Virgilio Jr. acknowledgement link.
- [ ] **Step 3: Implement the minimal checks and docs.** Document invocation as `$<skill-slug>` or a natural-language request matching its description; explain canonical ownership, symlink/copy installation, no overwrite, cache reuse, and how an existing reading system supplies `ChapterSnapshot`.
- [ ] **Step 4: Run complete phase gate.**

```bash
uv lock --check
uv run --no-sync pytest --cov=cove_book_forge --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy src/cove_book_forge
uv run --no-sync mypy examples/custom_provider.py examples/publish_chapter_skill.py
uv build --clear
uv run --no-sync python scripts/verify_distribution.py dist
git diff --check
git status --short
```

Commit: `docs: document generated book skills`

## Definition of Done

- One cached `AnalyzedChapter` produces a valid, concise, deterministic Skill without another Provider call.
- The required `SKILL.md`, Codex metadata, chapter reference, glossary, patterns, cheatsheet, and checksummed manifest are complete and safe.
- Repeating an unchanged publication is byte-for-byte no-op; updating one chapter preserves all other chapter references.
- Prompt-injection text remains inert reference content and cannot add Skill instructions, tools, scripts, paths, links, hooks, or MCP dependencies.
- Canonical publication is atomic/recoverable and never overwrites a non-managed or externally modified target.
- Selected Codex, Claude, and generic Agent targets discover the Skill by verified managed symlink or guarded copy; installation conflicts never damage the canonical Skill.
- Obsidian and Skill output orders reuse one analysis cache entry across restart.
- Doctor checks canonical/install readiness read-only and without revealing private paths.
- A generated fixture passes the repository scanner, the standard quick validator when available, and one context-isolated forward test.
- README/CHANGELOG and attribution accurately distinguish completed single-chapter Skill output from future whole-book jobs and MCP work.
- Full tests, coverage, Ruff, format, strict mypy, lock, build, distribution verification, and diff checks pass.
