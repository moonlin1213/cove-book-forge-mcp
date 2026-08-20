# Obsidian Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish one already validated `AnalyzedChapter` into deterministic Obsidian chapter notes, atomic cards, and a book MOC without another model call or unsafe overwrite.

**Architecture:** A pure `ObsidianRenderer` converts a snapshot plus the shared `ChapterAnalysis` into managed Markdown. A checksummed book manifest supplies stable cross-chapter MOC state. A reusable local publisher validates an explicitly configured vault root, detects external edits, stages every file, and either commits the whole bundle or restores the previous version.

**Tech Stack:** Python 3.11+, Pydantic 2, stdlib `hashlib`/`json`/`os`/`pathlib`/`shutil`/`tempfile`/`unicodedata`, pytest, Ruff, strict mypy.

**Spec:** `docs/superpowers/specs/2026-08-14-cove-book-forge-mcp-design.md` (§5, §9.2, §11, §14–17)

## Global Constraints

- Rendering consumes an existing `AnalyzedChapter`; it never calls a Provider and never performs analysis or repair.
- Output roots come only from the enabled `ObsidianOutputConfig`. Every target must remain under that exact authorized vault through all checks and writes.
- Reject absolute paths, `..`, empty/dot components, control characters, backslash ambiguity, symlink roots/components/targets, and root replacement races.
- Never overwrite a non-managed file. Update or remove a managed file only when its marker, book identity, and recorded body hash still match; otherwise return `EXTERNAL_MODIFICATION`.
- Chapter notes, cards, MOC, and the book manifest publish as one recoverable transaction. A failure leaves the last successful visible version intact and removes staging safely.
- Re-publishing the same fingerprint with unchanged files performs zero rewrites and returns `unchanged=True`.
- Markdown frontmatter contains only fixed `cove_*` fields with JSON-quoted scalar values. Source content cannot add frontmatter fields, choose paths, create links outside the vault, or introduce executable files.
- Stable path IDs derive from canonical source identity/chapter index/content identity, never from secrets. Human titles are sanitized display suffixes only.
- The manifest stores only controlled identity, coverage/index summaries, relative paths, and hashes; it does not store chapter body, private notes, raw prompts, Provider output, API keys, or absolute paths.
- This phase does not implement Agent Skill output/install, whole-book jobs, MCP tools/transports, UI, or live Provider requests.

## Locked Public Interfaces

Add and export:

```python
class ObsidianPublishResult(ContractModel):
    book_key: str
    chapter_path: str
    moc_path: str
    card_paths: tuple[str, ...] = ()
    input_fingerprint: str
    changed_paths: tuple[str, ...] = ()
    unchanged: bool = False


class ObsidianOutput:
    def __init__(self, config: ObsidianOutputConfig) -> None: ...

    def publish(
        self,
        snapshot: ChapterSnapshot,
        analyzed: AnalyzedChapter,
    ) -> ObsidianPublishResult: ...
```

Internal pure boundary:

```python
class RenderedObsidianBook:
    files: Mapping[str, bytes]
    manifest: ObsidianBookManifest
    chapter_path: str
    moc_path: str
    card_paths: tuple[str, ...]


class ObsidianRenderer:
    def render(
        self,
        snapshot: ChapterSnapshot,
        analyzed: AnalyzedChapter,
        previous: ObsidianBookManifest | None,
    ) -> RenderedObsidianBook: ...
```

## Locked Managed Format

- `book_key = sha256(canonical JSON composite [source_system, external_book_id])[:16]`; `+` denotes the canonical composite, never ambiguous bare string concatenation.
- Book directory: `<notes_folder>/<safe book title>--<book_key>/`.
- MOC: `<book directory>/<safe book title> MOC.md`.
- Chapter note: `<book directory>/Chapters/<index+1:02d> <safe chapter title>.md`.
- Cards: `<cards_folder>/<safe concept or rule title>--<stable card id>.md`.
- Manifest: `.cove-book-forge/obsidian/<book_key>.json`.
- Card stable IDs distinguish concepts and decision rules and include book identity plus chapter index. Duplicate terms/rules receive deterministic occurrence ordinals.
- Every Markdown file starts with exactly these controlled fields as applicable: `cove_book_forge`, `cove_schema`, `cove_kind`, `cove_book_key`, `cove_chapter_index`, `cove_source_fingerprint`, `cove_stable_id`, `cove_body_sha256`. The hash covers bytes after the closing frontmatter delimiter.
- Manifest schema v1 contains a checksum over its canonical JSON excluding `checksum`, book display metadata, and per-chapter fingerprint/note/card/framework/topic summaries needed to rebuild the MOC.

---

### Task 1: Render deterministic managed Markdown

**Files:**

- Modify: `src/cove_book_forge/config/models.py`
- Create: `src/cove_book_forge/contracts/outputs.py`
- Modify: `src/cove_book_forge/contracts/__init__.py`
- Create: `src/cove_book_forge/outputs/__init__.py`
- Create: `src/cove_book_forge/outputs/obsidian_models.py`
- Create: `src/cove_book_forge/outputs/obsidian_render.py`
- Create: `tests/test_obsidian_render.py`
- Modify: `tests/test_config.py`

**Interfaces:**

- Consumes: `ChapterSnapshot`, `AnalyzedChapter`, `ObsidianOutputConfig`.
- Produces: pure renderer, strict internal manifest models, stable relative paths, and public `ObsidianPublishResult` contract.

- [ ] **Step 1: Write failing render/config tests.** Cover strict/frozen result and manifest models; safe folder validation; stable book/card IDs; same identity under Unicode/line-ending variants; title collision separation; deterministic output bytes; exact controlled frontmatter; body hash verification; no source-created frontmatter; no secrets/absolute paths; empty analysis collections.
- [ ] **Step 2: Write golden content RED tests.** Prove a chapter note includes source, core idea, frameworks, concepts, mental models, methods, anti-patterns, rules, examples, takeaways, highlights, notes, annotations/reflections, evidence, quality warnings, and card links. Prove one concept/rule per card with links to the source chapter and MOC. Prove the MOC contains coverage, chapter directory, frameworks, topics, and cards.
- [ ] **Step 3: Run RED.**

```bash
uv run --no-sync pytest tests/test_obsidian_render.py tests/test_config.py -v
```

- [ ] **Step 4: Implement minimal pure renderer.** Normalize NFC/newlines, sanitize display filenames with deterministic fallbacks and length bounds, escape Markdown link labels/targets, JSON-quote frontmatter values, render fixed sections, compute body hashes, and canonicalize manifest/checksum. Do not parse or write the filesystem.
- [ ] **Step 5: Run gates and commit.**

```bash
uv run --no-sync pytest tests/test_obsidian_render.py tests/test_config.py tests/test_contract_analysis.py -v
uv run --no-sync ruff check src/cove_book_forge/outputs src/cove_book_forge/contracts src/cove_book_forge/config tests/test_obsidian_render.py tests/test_config.py
uv run --no-sync mypy src/cove_book_forge
git diff --check
```

Commit: `feat: render managed obsidian notes`

### Task 2: Validate managed manifests and external modifications

**Files:**

- Create: `src/cove_book_forge/outputs/managed.py`
- Modify: `src/cove_book_forge/outputs/obsidian_models.py`
- Modify: `src/cove_book_forge/outputs/obsidian_render.py`
- Create: `tests/test_managed_outputs.py`

**Interfaces:**

- Consumes: Task 1 managed Markdown and manifest format.
- Produces: pure parsing/validation and publication-plan functions used before any filesystem mutation.

- [ ] **Step 1: Write failing validation tests.** Cover valid round-trip; wrong/missing/duplicate frontmatter fields; malformed JSON scalar; body hash mismatch; wrong kind/book/chapter/stable ID; invalid manifest checksum/schema/path; duplicate paths; absolute/traversal/backslash/control paths; stale cards; and sanitized public errors with no file body or private path.
- [ ] **Step 2: Write update-plan RED tests.** Given a previous manifest plus current file bytes, prove unchanged output is a no-op; changed analysis replaces only its managed chapter/cards/MOC/manifest; renamed titles remove stale managed paths; missing/tampered managed files and any occupied new non-managed path fail `EXTERNAL_MODIFICATION`; another book's managed file is never adopted.
- [ ] **Step 3: Run RED.**

```bash
uv run --no-sync pytest tests/test_managed_outputs.py -v
```

- [ ] **Step 4: Implement strict pure validators/planner.** Parse only the locked frontmatter format, recompute hashes/checksums, validate every relative path before use, compare exact managed ownership, and return fixed safe `ForgeException` values without raw content/path/cause leakage. Never merge arbitrary Markdown.
- [ ] **Step 5: Run gates and commit.**

```bash
uv run --no-sync pytest tests/test_managed_outputs.py tests/test_obsidian_render.py tests/test_errors.py -v
uv run --no-sync ruff check src/cove_book_forge/outputs tests/test_managed_outputs.py tests/test_obsidian_render.py
uv run --no-sync mypy src/cove_book_forge
git diff --check
```

Commit: `feat: detect managed obsidian conflicts`

### Task 3: Publish a chapter atomically inside the authorized vault

**Files:**

- Create: `src/cove_book_forge/outputs/publisher.py`
- Create: `src/cove_book_forge/outputs/obsidian.py`
- Modify: `src/cove_book_forge/outputs/__init__.py`
- Create: `tests/test_output_publisher.py`
- Create: `tests/test_obsidian_output.py`

**Interfaces:**

- Consumes: Tasks 1–2 renderer, manifest, and update plan.
- Produces: public synchronous `ObsidianOutput.publish(...) -> ObsidianPublishResult`.

- [ ] **Step 1: Write failing authorization/security tests.** Disabled/missing/non-directory/unwritable/symlink vault; traversal; symlink parent/target; root or ancestor replacement before validation/stage/commit; non-managed target; manifest deletion/tamper; and secret-free/path-free errors.
- [ ] **Step 2: Write failing atomicity tests.** First publish, idempotent no-rewrite, normal update, stale-card removal, and injected failures during staging, backup, first/middle/last replace, stale removal, manifest replace, and cleanup. Assert last successful visible bundle is restored byte-for-byte, no unrelated file is touched, and stage/backup debris is removed only when ownership is proven.
- [ ] **Step 3: Run RED.**

```bash
uv run --no-sync pytest tests/test_output_publisher.py tests/test_obsidian_output.py -v
```

- [ ] **Step 4: Implement guarded publisher and service.** Anchor the existing vault using no-follow identity checks; validate/create only controlled descendants without following links; stage same-filesystem files; snapshot exact preconditions; back up old managed files; publish with atomic `os.replace`; roll back published paths in reverse order on any failure; update the manifest last; fsync files/directories where supported; map failures to fixed output errors. Do not expose an overwrite flag in v0.1.
- [ ] **Step 5: Run gates and commit.**

```bash
uv run --no-sync pytest tests/test_output_publisher.py tests/test_obsidian_output.py tests/test_managed_outputs.py tests/test_obsidian_render.py -v
uv run --no-sync ruff check src/cove_book_forge/outputs tests/test_output_publisher.py tests/test_obsidian_output.py
uv run --no-sync mypy src/cove_book_forge
git diff --check
```

Commit: `feat: publish obsidian bundles atomically`

### Task 4: Integrate, document, and verify Obsidian output

**Files:**

- Modify: `src/cove_book_forge/doctor.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `tests/test_cli_doctor.py`
- Create: `tests/test_obsidian_integration.py`

**Interfaces:**

- Consumes: all Tasks 1–3 and the existing persistent `ChapterAnalyzer` cache.
- Produces: the completed single-chapter “炼入 OB” application boundary consumed later by MCP/Cove.

- [ ] **Step 1: Write end-to-end RED tests.** Analyze once with a fake Provider and real disabled `BookLibrary`, publish to a temporary vault, recreate services, and prove a second OB publish performs zero Provider calls and zero rewrites. Change one note and prove one new analysis plus one atomic managed update. Publish two chapters and prove MOC coverage/index preservation. Confirm Agent Skill placeholder consumption of the same cached analysis still performs zero extra model calls.
- [ ] **Step 2: Write Doctor RED tests.** Disabled output reports non-blocking status. Enabled output checks the configured vault read-only: existing directory, no symlink, no broad/root target, no writes or sidecars, and safe output. Missing/unsafe vault fails without exposing its path.
- [ ] **Step 3: Implement integration and truthful docs.** README shows a short Python application example for `ChapterAnalyzer` then `ObsidianOutput.publish`; document folder layout, managed-file conflict behavior, no automatic Markdown merge, atomic rollback, and zero-call cache reuse. Keep Skill/jobs/MCP status accurate and preserve `book-to-skill` attribution.
- [ ] **Step 4: Run complete phase gate.**

```bash
uv lock --check
uv run --no-sync pytest --cov=cove_book_forge --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy src/cove_book_forge
uv run --no-sync mypy examples/custom_provider.py
uv build --clear
uv run --no-sync python scripts/verify_distribution.py dist
git diff --check
git status --short
```

Commit: `docs: document safe obsidian output`

## Definition of Done

- One cached `AnalyzedChapter` publishes a deterministic chapter note, stable concept/rule cards, and a book MOC without another Provider call.
- Repeating the same publication is a byte-for-byte no-op; changing one chapter updates only its managed files and the aggregate MOC/manifest.
- Multiple chapters of one book preserve coverage and links in a checksummed manifest without storing private source text.
- Non-managed files, externally modified managed files, unsafe paths, symlinks, and unauthorized roots are never overwritten.
- Any injected publication failure restores the previous visible bundle and leaves no unsafe staging/backup debris.
- Markdown frontmatter and links remain controlled even when source titles/content contain YAML, Markdown, HTML, or prompt-injection text.
- Doctor validates configured Obsidian readiness read-only and without revealing private paths.
- README/CHANGELOG and attribution accurately distinguish implemented Obsidian output from future Skill/jobs/MCP work.
- Full tests, coverage, Ruff, repository format, strict mypy, lock, build, distribution verification, and diff checks pass.
