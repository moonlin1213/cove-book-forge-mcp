# cove-book-forge-mcp

`cove-book-forge-mcp` is a local-first, open-source Python core for securely
normalizing books and stable external reading snapshots. The implemented phases
support deterministic EPUB/PDF ingestion, an optional local SQLite library,
explicit model Provider adapters, and reusable structured chapter analysis.

> **Status boundary:** standards-valid EPUB ingestion, text-layer PDF ingestion,
> the optional SQLite library, external `ChapterSnapshot` caching, model Provider
> adapters, validated per-chapter analysis with persistent fingerprint reuse,
> guarded Obsidian publication, persistent complete-book Agent Skill forging,
> and the MCP tools/resources over stdio or loopback Streamable HTTP are implemented.
> Cove's private adapter and reading UI remain future integrations.

This repository is independent of private Cove/栖渡 code and does not include an
official reading UI.

## Supported input and safety boundaries

### EPUB

- Standards-valid EPUB 2/3 archives are supported.
- Reading order comes from the OPF spine, not filenames or ZIP member order.
  EPUB navigation/NCX labels supply chapter titles when available.
- Every ZIP member is preflighted before book content is read. Absolute paths,
  parent traversal, backslash paths, encrypted entries, archive symlinks, nested
  archives, and configured expansion-limit violations are rejected.

### PDF

- PDFs must contain a meaningful text layer. Page text remains in physical page
  order and deterministic chapter ranges keep the normalized book addressable.
- Scanned or image-only PDFs fail explicitly with `OCR_REQUIRED`. The core does
  not download an OCR engine, call a remote service, or silently invoke an OCR
  fallback.
- Encrypted and malformed PDFs fail with closed, path-safe errors.

The default ingestion limits are:

| Boundary | Default |
| --- | ---: |
| Source file | 512 MiB |
| PDF pages | 5,000 |
| ZIP members | 10,000 |
| Total expanded ZIP content | 1 GiB |
| Expanded bytes per ZIP member | 128 MiB |
| ZIP compression ratio | 100:1 |

Sources are fingerprinted before and after extraction. A source that changes
during parsing fails with `SOURCE_CHANGED` and is not partially persisted.

## Optional local library

The local cache lives at the configured `library.data_dir` and uses
`library.sqlite3`. Initialization and schema migration are explicit and local.

- `COPY` stores a verified copy beneath the library data directory. The original
  source can later move or disappear without affecting the managed copy.
- `REFERENCE` stores the resolved source path and its fingerprint without
  copying the file. If the source changes or disappears, `source_available`
  becomes `False`; already-normalized chapters remain readable.
- Setting `library.enabled: false` disables managed file import. A complete,
  stable external `ChapterSnapshot` can still be upserted into the optional
  local cache, and survives a new library-service instance. The external system
  remains authoritative.

### Managed import with public APIs

```python
from pathlib import Path

from cove_book_forge.config import AppConfig
from cove_book_forge.contracts import ImportMode
from cove_book_forge.library import create_book_library

data_dir = Path("/absolute/path/to/local-library")
config = AppConfig.model_validate(
    {
        "library": {"enabled": True, "data_dir": data_dir},
        "model": {"provider": "unused-local", "model": "unused-local"},
    }
)
library = create_book_library(config)
imported = library.import_book(Path("/absolute/path/to/book.epub"), ImportMode.COPY)

stored = library.get_book(imported.book)
first_chapter = library.get_chapter(imported.book, 0)
print(stored.metadata.title, first_chapter.title)
```

Use `ImportMode.REFERENCE` to retain a reference rather than a managed copy.
`list_books()`, `get_book()`, and `get_chapter()` read the same normalized
contracts after constructing another service for the same data directory.

### External snapshot upsert while managed import is disabled

```python
from pathlib import Path

from cove_book_forge.config import AppConfig
from cove_book_forge.contracts import BookMetadata, ChapterContent, ChapterSnapshot
from cove_book_forge.library import create_book_library

config = AppConfig.model_validate(
    {
        "library": {
            "enabled": False,
            "data_dir": Path("/absolute/path/to/optional-external-cache"),
        },
        "model": {"provider": "unused-local", "model": "unused-local"},
    }
)
library = create_book_library(config)
book = library.upsert_chapter_snapshot(
    ChapterSnapshot(
        source_system="my-reader",
        external_book_id="stable-book-id",
        book=BookMetadata(title="External Book", total_chapters=1),
        chapter=ChapterContent(
            index=0,
            title="Chapter 1",
            content="Complete normalized chapter text.",
            source_locator="my-reader:chapter:0",
        ),
    )
)
print(library.get_chapter(book, 0).content)
```

## Reusable chapter analysis

`ChapterAnalyzer` turns one normalized `ChapterSnapshot` into the strict public
`ChapterAnalysis` contract through an injected async `ModelProvider`. It validates
every structured response against the contract schema and allows at most one
additional generation when the Provider reports invalid model output or the
returned object does not validate. Authentication, rate-limit, availability, and
configuration failures are not repaired and never trigger a fallback Provider.

The analyzer computes a stable fingerprint over normalized chapter title/body,
highlights, notes, annotations, reflections, analysis configuration, prompt and
generator versions, and the `ChapterAnalysis` schema. A matching persistent cache
entry returns with zero Provider calls, including after recreating both the
library and analyzer. Provider/model/base identity is ignored by default, and can
be included explicitly with `analysis.include_provider_in_fingerprint: true`.
API-key names and values never enter the fingerprint.

Long chapters are split losslessly at Markdown-aware boundaries. Ordered chunks
contain chapter content only; fenced code and Markdown tables remain atomic, even
when one block exceeds the configured limit. Each chunk receives a validated
analysis, followed by exactly one final merge that receives the validated chunk
analyses and the chapter's highlights, notes, annotations, and reflections. The
full body is not repeated in the merge, and only the final merged analysis is
cached under the complete chapter fingerprint.

```python
from cove_book_forge.analysis import ChapterAnalyzer

analyzer = ChapterAnalyzer(
    provider,
    library,
    config.analysis,
    config.model,
)
analyzed = await analyzer.analyze(snapshot)
print(analyzed.analysis.core_idea, analyzed.cache_hit)
```

The same `AnalyzedChapter` feeds both implemented outputs without re-analysis.
An existing reading system supplies the complete `ChapterSnapshot`; its local
cache supplies the matching `AnalyzedChapter`, so a Skill publication does not
call a Provider again.

## Persistent complete-book Skill forging

`WholeBookForge` accepts either a managed library `book_id` or a complete ordered
sequence of external `ChapterSnapshot` values. Planning creates a secret-free
30-minute `ForgePlan` bound to every chapter analysis fingerprint, the selected
Provider/model, prompt and generator versions, and Skill output configuration.
Its estimate reports tokens and model calls only for uncached analyses; it does
not invent prices.

Starting a plan requires explicit confirmation and an idempotency key. One
controller owns a book's Skill at a time. The persistent SQLite journal records
plans, jobs, and chapter publication checkpoints. Each chapter is analyzed
through `ChapterAnalyzer` and published through `AgentSkillOutput`, so cache hits
make no model calls and the final Skill contains the complete chapter set.
Pause and cancellation take effect at chapter boundaries; cancellation preserves
already published chapters. Interrupted or failed jobs can resume/retry without
repeating completed checkpoints, including after process restart.

## MCP server

Install the project and start the default stdio server with:

```console
cove-book-forge mcp --config /absolute/path/to/config.yaml
```

The server exposes library import/read operations, chapter analysis and outputs,
whole-book planning/jobs/control/status, generated Skill discovery, and matching
`cove-book-forge://` resources. All tools return Pydantic-defined structured
results. Public errors use safe `ForgeErrorDetail` data and do not reveal source
paths, chapter text, Provider responses, or credentials.

An explicit local HTTP transport is also available:

```console
cove-book-forge mcp --transport http --host 127.0.0.1 --port 8000 \
  --config /absolute/path/to/config.yaml
```

Unauthenticated Streamable HTTP is restricted to loopback addresses. The server
does not provide a remote unauthenticated mode. Applications can construct an
`AppContext` with a custom `ModelProvider`; built-in DeepSeek configuration uses
the existing OpenAI-compatible Provider route.

## Safe Obsidian output

`ObsidianOutput` publishes one already analyzed chapter synchronously. The vault
must already exist and be explicitly configured; disk roots, the home directory,
the current working directory and its broad ancestors, symlinked paths, and
unwritable locations fail closed. Publication does not call a Provider or repair
an analysis.

This application-layer function is a type-valid composition of the public async
analysis boundary and synchronous output boundary:

```python
from cove_book_forge.analysis import ChapterAnalyzer
from cove_book_forge.config import AppConfig
from cove_book_forge.contracts import ChapterSnapshot, ObsidianPublishResult
from cove_book_forge.library import BookLibrary
from cove_book_forge.outputs import ObsidianOutput
from cove_book_forge.providers import ModelProvider


async def publish_chapter_to_obsidian(
    provider: ModelProvider,
    library: BookLibrary,
    config: AppConfig,
    snapshot: ChapterSnapshot,
) -> ObsidianPublishResult:
    analyzer = ChapterAnalyzer(provider, library, config.analysis, config.model)
    analyzed = await analyzer.analyze(snapshot)
    return ObsidianOutput(config.outputs.obsidian).publish(snapshot, analyzed)
```

A matching persistent input fingerprint makes `analyzer.analyze(...)` a
zero-call cache hit, including after the application recreates its library,
analyzer, and output services. Publishing the unchanged result performs no file
rewrite. Provider/model/base identity is excluded from the fingerprint by
default; set `analysis.include_provider_in_fingerprint: true` when an application
requires provider changes to invalidate cached analysis.

The deterministic managed layout is:

```text
<Vault>/
├── Books/<safe book title>--<stable book key>/
│   ├── <initial safe book title> MOC.md
│   └── Chapters/<01-based index> <safe chapter title>.md
├── Cards/<safe concept or rule title>--<stable card id>.md
└── .cove-book-forge/obsidian/<stable book key>.json
```

The checksummed manifest preserves chapter coverage, index entries, cards,
frameworks, and topics across separate chapter publications and process
restarts. Human-facing title changes update managed display metadata while the
physical book root and MOC path remain stable. The manifest stores controlled
identities, summaries, relative paths, and hashes—not source chapter bodies,
private notes, prompts, Provider responses, credentials, or absolute paths.

Every managed Markdown file has fixed `cove_*` frontmatter, including its
ownership identity and body hash. Cove Book Forge updates only files whose
managed marker, identity, and recorded hash still match. Editing a managed note
outside the application causes `EXTERNAL_MODIFICATION`; v0.1 does not merge
arbitrary Markdown and has no overwrite flag. Keep personal prose in separate,
non-managed notes and link to the generated material.

Chapter notes, relevant concept/rule cards, the aggregate MOC, and manifest are
staged and committed as one recoverable bundle, with the manifest last. A failed
publication restores the last successful visible bundle when it can prove
ownership and never adopts or deletes a competing file. Successful unchanged
publication is a byte-for-byte no-op.

The private Cove reading interface can call the public MCP application boundary
implemented in this repository. The core remains UI-agnostic: other reading
systems can use the same tools and resources, while users without an existing
reader can build their own interface around them.

## Generated Agent Skills

`AgentSkillOutput` turns one already analyzed `ChapterSnapshot` into a managed,
single-chapter Agent Skill. Configure an existing canonical directory and only
the conventional discovery roots you want to use:

```yaml
outputs:
  skills:
    enabled: true
    canonical_path: /absolute/path/to/generated-skills
    install_to: [codex, claude, agents]
```

The public synchronous boundary mirrors the Obsidian boundary. It accepts a
cached analysis and therefore never invokes a Provider:

```python
from cove_book_forge.contracts import AnalyzedChapter, ChapterSnapshot
from cove_book_forge.outputs import AgentSkillOutput

result = AgentSkillOutput(config.outputs.skills).publish(snapshot, analyzed)
print(result.skill_slug, result.canonical_path)
```

Run [`examples/publish_chapter_skill.py`](examples/publish_chapter_skill.py)
for a complete, local demonstration. The canonical root remains the source of
truth and has a guarded, recoverable managed layout. Each selected target is
installed as a verified relative symlink when supported; if symlinks are not
available, it receives a verified managed copy. Existing non-managed files,
directories, and links are never overwritten: an installation collision returns
`INSTALL_CONFLICT` while leaving the canonical Skill usable. Repeating an
unchanged publication reuses the cached analysis and performs a byte-for-byte
no-op.

After installation, invoke the generated skill as `$<skill-slug>` (for example,
`$durable-decisions--0123abcd0123abcd`) or make a natural-language request
matching the generated Skill's description. Codex uses `~/.codex/skills`,
Claude Code uses `~/.claude/skills`, and generic agents use `~/.agents/skills`;
only roots named in `install_to` are inspected or changed.

The same output boundary is used by the whole-book forge, which analyzes and
publishes chapters through persistent, resumable jobs. The public MCP server
exposes both chapter-level and whole-book workflows; only the private Cove UI
adapter remains outside this open-source repository.

## Model Providers

The async Provider boundary supports `openai`, `openai-compatible`, `deepseek`,
and `anthropic` as exact built-in names. It returns typed per-call results and
cumulative input/output/total-token usage. Each generation call makes at most
one request: there is no automatic retry, JSON repair, or fallback to another
provider inside an adapter. The implemented chapter-analysis layer owns its
separate bounded schema validation/regeneration policy described above. Trusted
provider usage is counted even when a charged response later fails termination,
content, or JSON-object validation.

API-key values are read only from the named process environment variable. Put
the variable name—not the key—in YAML, and provide the value through your shell,
service manager, or secrets manager. Keys, prompts, URLs, and raw response bodies
are excluded from public errors and are not logged by the Provider layer.
`openai`, `deepseek`, and `anthropic` require a configured, non-blank credential;
generic `openai-compatible` and explicitly registered custom Providers may omit it.

### DeepSeek

DeepSeek uses the OpenAI-compatible chat-completions protocol and the built-in
DeepSeek API base:

```yaml
model:
  provider: deepseek
  model: deepseek-chat
  api_key_env: DEEPSEEK_API_KEY
```

### OpenAI-compatible or self-hosted gateway

An explicit `base_url` is required. `api_key_env` is optional for a local gateway;
when configured, its environment value must be non-empty.

```yaml
model:
  provider: openai-compatible
  model: local-reader-model
  base_url: http://127.0.0.1:11434/v1
  # json_mode: true  # opt in only when this gateway supports native JSON Mode
  # api_key_env: LOCAL_MODEL_API_KEY
```

### Anthropic Claude

Claude uses the Anthropic Messages API:

```yaml
model:
  provider: anthropic
  model: claude-sonnet-4-5
  api_key_env: ANTHROPIC_API_KEY
```

OpenAI and DeepSeek default to native JSON-object mode. Generic
`openai-compatible` defaults to prompt-only JSON; set optional `json_mode: true`
only for a gateway that supports `response_format`, or set `false` to disable
native mode for OpenAI or DeepSeek. Every path adds the controlled direct-object
instruction and strictly accepts one JSON object. Anthropic always advertises
native JSON mode and JSON Schema as unavailable and ignores an OpenAI-style
override. Output capability maxima remain unknown; `default_max_output_tokens`
is a caller default, not a vendor hard limit. Domain-schema validation and one
bounded invalid-output regeneration are supplied by `ChapterAnalyzer`, not the
Provider adapter.

Built-in Providers created by one application-owned `ProviderRegistry` share
configured concurrency and 60-second request limits across models on the same
route and credential identity. Each instance keeps its own cumulative usage.
Separate registries are isolated, so an embedding application should reuse its
registry for one application boundary.

### Explicit custom Provider registration

Applications can register a typed local or proprietary Provider explicitly,
without plugin discovery or dynamic import paths:

```python
from your_application.providers import custom_provider_factory

from cove_book_forge.config import ModelConfig
from cove_book_forge.providers import ProviderRegistry

registry = ProviderRegistry({"deterministic-local": custom_provider_factory})
provider = registry.create(ModelConfig(provider="deterministic-local", model="reader-model"))
```

See the repository/sdist source example
[`examples/custom_provider.py`](examples/custom_provider.py) for a complete typed
implementation. It is source material, not a wheel package namespace; copy or
adapt it under your own application package before using the import above. The
standalone `doctor` command recognizes only the exact built-ins; an embedding
application owns readiness checks for its explicitly registered custom Providers.

## Privacy defaults and diagnostics

- Library data and normalized snapshots stay local.
- Telemetry, cloud sync, remote logging, and hidden network fallbacks are
  disabled.
- API-key values come from environment variables and are never stored in YAML.
- Configured output roots require explicit authorization. Obsidian publication
  remains local and writes only its validated managed bundle beneath that vault.

The `doctor` command is read-only and network-free. It checks configuration,
built-in Provider/base readiness, required environment-variable presence,
EPUB/PDF parser dependencies, configured library-directory readiness, any
existing `library.sqlite3` through a read-only SQLite integrity check, and the
enabled Obsidian vault through the same narrow no-follow readiness boundary used
by publication. It never renders or publishes output, calls Provider generation
or health-check APIs, or creates directories, databases, journals, WAL files,
temporary files, manifests, or migrations. Disabled Obsidian output is a
non-failing warning; a configured missing, broad, linked, or unwritable vault is
a safe path-free failure.

```bash
uv sync --group dev
uv run cove-book-forge doctor --config /absolute/path/config.yaml
```

If managed import is disabled and the optional external cache does not yet
exist, `doctor` reports a non-failing warning when its existing parent is ready.
Unsafe, unreadable, or invalid existing databases remain failures.

## Contributor verification

Run the complete local verification sequence before submitting a change:

```bash
uv lock --check
uv run --no-sync pytest --cov=cove_book_forge --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy src/cove_book_forge
uv run --no-sync mypy examples/custom_provider.py
uv build --clear
uv run --no-sync python scripts/verify_distribution.py dist
```

Provider tests use local mocked transports and deterministic fake Providers only;
the test suite performs no live model request and requires no real account or key.

## Acknowledgements

`cove-book-forge-mcp` is inspired by and builds upon ideas and tooling from
[book-to-skill](https://github.com/virgiliojr94/book-to-skill), created by
**Virgilio Jr.** We are grateful for its document-extraction work, Agent Skill
structure, and open-source contribution. The concrete adapted sanitizer notice
is preserved in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT. See [LICENSE](LICENSE) and the bundled third-party terms in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
