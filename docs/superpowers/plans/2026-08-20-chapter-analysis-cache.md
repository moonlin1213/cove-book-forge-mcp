# Chapter Analysis and Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a normalized `ChapterSnapshot` into one validated, reusable `ChapterAnalysis`, persist it by a stable input fingerprint, and make unchanged chapters perform zero model calls.

**Architecture:** Deterministic normalization and fingerprinting stay separate from model prompts. `ChapterAnalyzer` consumes the public async `ModelProvider` and a small synchronous cache protocol; `BookLibrary` implements that cache over the existing SQLite database. Long chapters are split without truncating atomic code/table blocks, analyzed in bounded pieces, then merged once into the same public `ChapterAnalysis` contract.

**Tech Stack:** Python 3.11+, Pydantic 2, asyncio, stdlib `hashlib`/`json`/`sqlite3`/`unicodedata`, existing model Provider boundary, pytest, Ruff, strict mypy.

**Spec:** `docs/superpowers/specs/2026-08-14-cove-book-forge-mcp-design.md` (§8.3, §9, §10, §13, §15.3, §17)

## Global Constraints

- The fingerprint covers normalized chapter title/body, highlights, user notes, annotations, reflections, analysis configuration, prompt-template version, generator version, and the `ChapterAnalysis` JSON schema.
- Provider/model/base identity enters the fingerprint only when `analysis.include_provider_in_fingerprint` is true; API-key names and values never enter it.
- A matching cached fingerprint returns without calling the Provider. Provider changes do not invalidate default caches; prompt/schema/generator changes always do.
- Book content is untrusted data. Prompts separate system instructions, schema, and source payload; source text can never choose tools, paths, provider, or executable behavior.
- Structured repair is bounded to one additional Provider call. Authentication, rate-limit, and availability failures are never repaired or routed to another Provider.
- Long chapters are never truncated. Splitting follows headings/paragraph blocks and keeps fenced code/table blocks atomic; a single oversized atomic block stays whole.
- Only a fully validated final `ChapterAnalysis` is cached. Chunk results and failed/partial repairs never enter persistent cache.
- Cache storage works when `library.enabled` is false, because the optional SQLite cache remains available for external snapshots.
- Public errors stay closed and never contain chapter text, notes, annotations, reflections, prompts, raw model output, private paths, API keys, or SQL.
- No Obsidian rendering, Agent Skill rendering/install, whole-book job orchestration, MCP transport, UI, or live Provider request is added in this phase.

## Locked Interfaces

Add `AnalysisConfig` to `config/models.py` and `AppConfig.analysis` with backward-compatible defaults:

```python
class AnalysisConfig(ConfigModel):
    prompt_template_version: str = Field(default="chapter-analysis-v1", min_length=1, max_length=120)
    generator_version: str = Field(default="cove-analysis-v1", min_length=1, max_length=120)
    max_chunk_characters: int = Field(default=24_000, ge=128, le=1_000_000)
    include_provider_in_fingerprint: bool = False
```

Add and export this result contract from `contracts/analysis.py`:

```python
class AnalyzedChapter(ContractModel):
    analysis: ChapterAnalysis
    input_fingerprint: str
    cache_hit: bool
```

Add the analysis package with these public/internal boundaries:

```python
class ChapterAnalysisCache(Protocol):
    def load_chapter_analysis(
        self, source_system: str, external_book_id: str, chapter_index: int,
        input_fingerprint: str,
    ) -> ChapterAnalysis | None: ...

    def store_chapter_analysis(
        self, source_system: str, external_book_id: str, chapter_index: int,
        input_fingerprint: str, analysis: ChapterAnalysis,
    ) -> None: ...

def chapter_input_fingerprint(
    snapshot: ChapterSnapshot,
    analysis_config: AnalysisConfig,
    model_config: ModelConfig,
) -> str: ...

class ChapterAnalyzer:
    def __init__(
        self,
        provider: ModelProvider,
        cache: ChapterAnalysisCache,
        analysis_config: AnalysisConfig,
        model_config: ModelConfig,
    ) -> None: ...

    async def analyze(self, snapshot: ChapterSnapshot, *, force: bool = False) -> AnalyzedChapter: ...
```

---

### Task 1: Add analysis configuration, canonical fingerprints, and safe prompt/chunk inputs

**Files:**

- Modify: `src/cove_book_forge/config/models.py`
- Modify: `src/cove_book_forge/config/__init__.py`
- Modify: `src/cove_book_forge/contracts/analysis.py`
- Modify: `src/cove_book_forge/contracts/__init__.py`
- Create: `src/cove_book_forge/analysis/__init__.py`
- Create: `src/cove_book_forge/analysis/fingerprint.py`
- Create: `src/cove_book_forge/analysis/chunks.py`
- Create: `src/cove_book_forge/analysis/prompts.py`
- Create: `tests/test_analysis_fingerprint.py`
- Create: `tests/test_analysis_chunks.py`
- Create: `tests/test_analysis_prompts.py`

**Interfaces:**

- Consumes: `ChapterSnapshot`, `ChapterAnalysis.model_json_schema()`, `ModelConfig` without credentials.
- Produces: `AnalysisConfig`, `AnalyzedChapter`, `chapter_input_fingerprint`, `split_chapter_content`, and prompt builders consumed by Tasks 3–4.

- [ ] **Step 1: Write failing contract/fingerprint tests.** Prove defaults and bounds; strict/frozen `AnalyzedChapter`; lowercase SHA-256; CRLF→LF and Unicode NFC normalization; supplemental items sorted by stable `id`; order-only changes do not invalidate; title/body/note/config/template/generator/schema changes do invalidate; provider/model/base changes invalidate only when opted in; `api_key_env` never affects or appears in the canonical payload.
- [ ] **Step 2: Run RED.**

```bash
uv run --no-sync pytest tests/test_analysis_fingerprint.py -v
```

Expected: import/attribute failures because the analysis config and fingerprint module do not exist.

- [ ] **Step 3: Implement canonical normalization and fingerprinting.** Use `unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))`, canonical JSON with sorted keys/compact separators/`allow_nan=False`, stable-id sorting for highlights/notes/annotations/reflections, and SHA-256 of the exact canonical bytes. Include a SHA-256 of the canonical `ChapterAnalysis.model_json_schema()`; never include book metadata, source identity, key env, or key value in the content component.
- [ ] **Step 4: Write and run failing chunk/prompt tests.** Prove headings/paragraphs split deterministically, fenced code and contiguous Markdown tables remain atomic, no character is lost/reordered, oversized atomic blocks stay whole, and prompt payloads label source as untrusted JSON data while preserving Chinese/Unicode and excluding config secrets.
- [ ] **Step 5: Implement minimal chunk/prompt modules.** `split_chapter_content(content, max_characters) -> tuple[str, ...]` builds blocks first and packs them greedily; it never slices a fenced-code/table block. Prompt builders return separate `(system_prompt, user_prompt)` strings, include the compact `ChapterAnalysis` schema, and use fixed text instructing the model to treat source content only as data and never invent evidence.
- [ ] **Step 6: Run Task 1 gates and commit.**

```bash
uv run --no-sync pytest tests/test_analysis_fingerprint.py tests/test_analysis_chunks.py tests/test_analysis_prompts.py tests/test_contract_analysis.py tests/test_config.py -v
uv run --no-sync ruff check src/cove_book_forge/analysis src/cove_book_forge/config src/cove_book_forge/contracts tests/test_analysis_fingerprint.py tests/test_analysis_chunks.py tests/test_analysis_prompts.py
uv run --no-sync mypy src/cove_book_forge
git diff --check
```

Commit: `feat: add deterministic chapter analysis inputs`

### Task 2: Persist validated chapter analyses in the optional SQLite cache

**Files:**

- Modify: `src/cove_book_forge/library/database.py`
- Modify: `src/cove_book_forge/library/repository.py`
- Modify: `src/cove_book_forge/library/service.py`
- Modify: `src/cove_book_forge/library/__init__.py`
- Modify: `src/cove_book_forge/doctor.py`
- Modify: `tests/test_library_database.py`
- Modify: `tests/test_cli_doctor.py`
- Create: `tests/test_analysis_cache.py`

**Interfaces:**

- Consumes: `ChapterAnalysisCache` from Task 1 and canonical `ChapterAnalysis` JSON.
- Produces: `BookLibrary.load_chapter_analysis(...)` and `BookLibrary.store_chapter_analysis(...)`, so a `BookLibrary` instance satisfies `ChapterAnalysisCache` directly.

- [ ] **Step 1: Write failing migration/cache tests.** Cover v2→v3 migration, fresh v3 creation, repeat initialization, existing v1→v3 data preservation, future/incomplete schema rejection, disabled-library cache operation, cache hit/miss by full stable identity+chapter+fingerprint, overwrite on changed fingerprint, canonical round-trip, corrupt cached JSON fail-closed, transactional rollback, and private-content-free errors.
- [ ] **Step 2: Run RED.**

```bash
uv run --no-sync pytest tests/test_analysis_cache.py tests/test_library_database.py tests/test_cli_doctor.py -v
```

Expected: schema version/table/method failures; existing v1/v2 readiness tests remain green until their expected current-version assertions are updated.

- [ ] **Step 3: Add schema v3.** Create exactly one new table:

```sql
CREATE TABLE chapter_analyses (
    source_system TEXT NOT NULL,
    external_book_id TEXT NOT NULL,
    chapter_index INTEGER NOT NULL CHECK (chapter_index >= 0),
    input_fingerprint TEXT NOT NULL,
    analysis_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_system, external_book_id, chapter_index)
)
```

Migrate v1 through existing snapshot migration and then create `chapter_analyses`; migrate v2 by creating only this table; set `PRAGMA user_version = 3` in the same transaction. Update the shared schema inspector/Doctor so v1/v2 are migration-pending, v3 is ready, and future/incomplete objects fail closed.
- [ ] **Step 4: Implement repository and guarded BookLibrary cache methods.** Validate lowercase 64-char fingerprints, canonicalize analysis JSON before storage, use one transaction/upsert, revalidate stored JSON through `ChapterAnalysis.model_validate`, and reuse the existing data-root identity guard. A fingerprint mismatch is a normal miss; malformed storage raises a fixed safe `MODEL_OUTPUT_INVALID` without the row content.
- [ ] **Step 5: Run Task 2 gates and commit.**

```bash
uv run --no-sync pytest tests/test_analysis_cache.py tests/test_library_database.py tests/test_external_snapshots.py tests/test_cli_doctor.py -v
uv run --no-sync ruff check src/cove_book_forge/library src/cove_book_forge/doctor.py tests/test_analysis_cache.py tests/test_library_database.py tests/test_cli_doctor.py
uv run --no-sync mypy src/cove_book_forge
git diff --check
```

Commit: `feat: cache validated chapter analyses`

### Task 3: Generate, validate, repair once, and reuse chapter analysis

**Files:**

- Create: `src/cove_book_forge/analysis/cache.py`
- Create: `src/cove_book_forge/analysis/service.py`
- Modify: `src/cove_book_forge/analysis/__init__.py`
- Create: `tests/test_chapter_analyzer.py`

**Interfaces:**

- Consumes: Task 1 fingerprint/prompts, Task 2 cache protocol implementation, `ModelProvider.generate_json`, `ChapterAnalysis`.
- Produces: public `ChapterAnalyzer.analyze(snapshot, force=False) -> AnalyzedChapter` for both later output renderers.

- [ ] **Step 1: Write failing analyzer tests with a typed fake Provider/cache.** Cover valid generation; matching cache hit with exactly zero Provider calls; changed input/config/template/schema miss; provider change hit by default and miss when opted in; `force=True`; invalid schema repaired by exactly one additional call; first `MODEL_OUTPUT_INVALID` regenerated once; second invalid result returns closed `MODEL_OUTPUT_INVALID`; auth/rate/unavailable errors propagate with zero repair; only final validated result is cached; prompt/raw output/source text never enters public errors.
- [ ] **Step 2: Add a concurrent same-key RED test.** Two simultaneous `analyze()` calls on one service instance must share one in-flight generation and return the same final analysis; a failure releases/removes the per-key lock so a later call can retry.
- [ ] **Step 3: Run RED.**

```bash
uv run --no-sync pytest tests/test_chapter_analyzer.py -v
```

Expected: missing `ChapterAnalyzer`/cache protocol.

- [ ] **Step 4: Implement minimal service.** Compute fingerprint before the lock, check cache, acquire a per-key async lock, recheck cache, then call `generate_json` with `ChapterAnalysis.model_json_schema()` and `model_config.default_max_output_tokens`. Validate with `ChapterAnalysis.model_validate`. On schema/JSON invalidity only, make one repair/regeneration call; on its failure raise fixed `MODEL_OUTPUT_INVALID`. Cache only the final validated object. Remove idle locks in `finally`; never hold a global lock during Provider or SQLite work.
- [ ] **Step 5: Run Task 3 gates and commit.**

```bash
uv run --no-sync pytest tests/test_chapter_analyzer.py tests/test_provider_contracts.py tests/test_analysis_cache.py -v
uv run --no-sync ruff check src/cove_book_forge/analysis tests/test_chapter_analyzer.py
uv run --no-sync mypy src/cove_book_forge
git diff --check
```

Commit: `feat: analyze and reuse unchanged chapters`

### Task 4: Analyze long chapters, integrate, document, and verify

**Files:**

- Modify: `src/cove_book_forge/analysis/service.py`
- Modify: `src/cove_book_forge/analysis/prompts.py`
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Create: `tests/test_long_chapter_analysis.py`
- Create: `tests/test_analysis_integration.py`

**Interfaces:**

- Consumes: all Tasks 1–3 interfaces.
- Produces: the completed reusable analysis phase consumed unchanged by future `ObsidianRenderer` and `AgentSkillRenderer`.

- [ ] **Step 1: Write failing long-chapter tests.** With an injected small chunk limit, prove each content character appears in exactly one ordered chunk prompt, supplemental highlights/notes/annotations/reflections appear only in the final merge prompt, code/table blocks are not split, N chunks cause N analysis calls plus one merge call, every call is schema validated with at most one repair, and no partial chunk result is cached after a later failure.
- [ ] **Step 2: Write failing end-to-end cache tests.** Initialize a real optional library with `library.enabled: false`, analyze a Unicode external snapshot with a fake Provider, recreate library/analyzer instances, and prove the second call is a persistent cache hit with zero Provider calls. Change one note and prove exactly one new final analysis is stored. Analyze the same snapshot for “OB” then “Skill” consumers and prove both receive the identical cached `ChapterAnalysis`.
- [ ] **Step 3: Run RED.**

```bash
uv run --no-sync pytest tests/test_long_chapter_analysis.py tests/test_analysis_integration.py -v
```

Expected: single-call analyzer behavior does not yet chunk/merge.

- [ ] **Step 4: Implement chunk analysis and one final merge.** For one chunk, send the complete snapshot once. For multiple chunks, analyze ordered content-only chunks, then send canonical chunk analyses plus the snapshot’s supplemental items to one merge prompt. Validate/repair each call through the same bounded helper. Store only the merged result under the original full-input fingerprint.
- [ ] **Step 5: Update docs truthfully.** README/CHANGELOG must state that validated per-chapter analysis, persistent fingerprint cache, one bounded repair, and long-chapter merge are implemented; Provider/output/MCP boundaries remain accurate; `book-to-skill`/Virgilio Jr. acknowledgement stays visible. Document that unchanged chapters cause zero Provider calls and default fingerprints ignore Provider identity.
- [ ] **Step 6: Run the complete phase gate.**

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

Commit: `docs: document reusable chapter analysis`

## Definition of Done

- A normalized `ChapterSnapshot` produces one strict `ChapterAnalysis` through the configured public Provider.
- An identical fingerprint returns the persisted analysis with zero Provider calls, including after recreating the library/analyzer service.
- Title/body/highlight/note/annotation/reflection/config/template/generator/schema changes invalidate exactly that chapter; Provider changes do not unless opted in.
- Invalid model structure receives at most one repair/regeneration call; auth/rate/unavailable errors receive none.
- Long chapters are never truncated and cache only one final merged analysis.
- The optional cache works with managed import disabled and remains safe/transactional under migration, rollback, corruption, and concurrent same-key analysis.
- Both future output paths can consume the same `AnalyzedChapter.analysis` without re-analysis.
- Full tests, Ruff, repository-wide format, strict mypy, lock, build, distribution verification, attribution, and documentation checks pass.
