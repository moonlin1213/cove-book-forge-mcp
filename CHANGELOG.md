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
