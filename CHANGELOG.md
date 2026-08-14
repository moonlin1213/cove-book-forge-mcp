# Changelog

## Unreleased

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
- Extended `doctor` with read-only parser-dependency, library-directory, and
  SQLite readiness checks that do not initialize or migrate storage.
