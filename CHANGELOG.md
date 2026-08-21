# Changelog

## Unreleased

- Established the independent project design and public attribution to
  [book-to-skill](https://github.com/virgiliojr94/book-to-skill) by Virgilio Jr.
- Added an installable local-first foundation with public contracts,
  configuration, authorized path policy, and a read-only `doctor` command.
- Added continuous-integration coverage for Python 3.11, 3.12, 3.13, and 3.14.
- Added secure, deterministic standards-valid EPUB ingestion in spine order,
  including archive preflight and bounded XHTML normalization.
- Added deterministic text-layer PDF ingestion with explicit `OCR_REQUIRED`
  classification for scanned or image-only documents and no hidden OCR fallback.
- Added an optional transactional SQLite library with `COPY` and `REFERENCE`
  import modes, ordered normalized chapters, source fingerprinting, and source
  availability reporting.
- Added stable external `ChapterSnapshot` upserts while managed import is
  disabled, plus an idempotent schema migration that fingerprints stored
  snapshots.
- Added one async model Provider boundary with exact OpenAI, OpenAI-compatible,
  DeepSeek, and Anthropic adapters; explicit custom registration; bounded
  transport; safe error mapping; and typed token-usage accounting.
- Extended `doctor` with network-free built-in Provider/base and credential
  readiness alongside read-only parser-dependency, library-directory, and SQLite
  checks that do not initialize or migrate storage.
- Added strict reusable `ChapterAnalysis` generation with deterministic input
  fingerprints, persistent zero-call cache hits, one bounded invalid-output
  regeneration, and same-key singleflight behavior. Provider identity remains
  outside the default fingerprint unless explicitly enabled.
- Added lossless long-chapter analysis: ordered content-only chunks preserve
  fenced code and Markdown tables, then exactly one validated merge combines the
  canonical chunk analyses with supplemental reader data. Only the final merged
  analysis is cached.
- Added guarded single-chapter Obsidian output with deterministic managed chapter
  notes, stable concept/rule cards, aggregate book MOCs, checksummed manifests,
  external-modification detection, unchanged no-rewrite publication, and
  recoverable atomic bundle updates inside an explicitly authorized vault.
- Extended `doctor` with a read-only, path-safe Obsidian readiness check that
  reuses the publisher's narrow vault validation without rendering, staging, or
  writing.
- Added deterministic, guarded single-chapter Agent Skill publication with
  verified optional Codex, Claude Code, and generic Agent installation.
  `doctor` now probes configured Skill roots read-only without creating output
  state.
- Added persistent complete-book Agent Skill forging from managed books or
  complete external snapshots, including fingerprint-bound 30-minute plans,
  cache-aware token/call estimates, explicit confirmation, per-book idempotent
  control, chapter checkpoints, boundary pause/cancel, retry, and restart resume.
- Added the official Python MCP SDK server with strict structured tools for the
  library, analysis, chapter outputs, complete-book jobs, and Skill discovery;
  corresponding `cove-book-forge://` resources; stdio by default; and explicit
  loopback-only Streamable HTTP. Custom Providers remain injectable through the
  application context. Cove's private adapter and UI remain future work.
