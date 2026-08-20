# cove-book-forge-mcp

`cove-book-forge-mcp` is a local-first, open-source Python core for securely
normalizing books and stable external reading snapshots. The implemented phases
support deterministic EPUB/PDF ingestion, an optional local SQLite library, and
explicit model Provider adapters for raw text or JSON-object generation.

> **Status boundary:** standards-valid EPUB ingestion, text-layer PDF ingestion,
> the optional SQLite library, external `ChapterSnapshot` caching, and model
> Provider adapters are implemented. Chapter analysis/fingerprinting/cache,
> forge/jobs, Obsidian output, Agent Skill generation/installation, and MCP
> transport remain planned and are **not implemented**.

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

## Model Providers

The async Provider boundary supports `openai`, `openai-compatible`, `deepseek`,
and `anthropic` as exact built-in names. It returns typed per-call results and
cumulative input/output/total-token usage. Each generation call makes at most
one request: there is no automatic retry, JSON repair, or fallback to another
provider. Chapter-level prompts and schema validation belong to the planned
analysis layer. Trusted provider usage is counted even when a charged response
later fails termination, content, or JSON-object validation.

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
is a caller default, not a vendor hard limit. Bounded repair and domain-schema
validation remain planned analysis behavior.

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
- Configured output roots require explicit authorization, although output
  generation is not part of the implemented phase.

The `doctor` command is read-only and network-free. It checks configuration,
built-in Provider/base readiness, required environment-variable presence,
EPUB/PDF parser dependencies, configured library-directory readiness, and any
existing `library.sqlite3` through a read-only SQLite integrity check. It never
calls Provider generation or health-check APIs, and does not create directories,
databases, journals, WAL files, temporary files, or migrations.

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
