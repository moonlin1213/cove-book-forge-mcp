# Secure EPUB/PDF Ingestion and Optional Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Every task follows red-green-refactor and ends in a focused commit.

**Goal:** Reliably turn local EPUB and text-layer PDF files into normalized books and chapters, persist them in an optional SQLite library, and accept stable external chapter snapshots when the managed library is disabled.

**Architecture:** Deterministic extractors produce public ingestion contracts and never call a model. A small application service validates and fingerprints the source before and after extraction, then writes through a transactional SQLite repository. External systems use the same normalized chapter model but remain the source of truth. Advanced PDF layout recovery and OCR are explicit extension points, not hidden fallbacks.

**Tech Stack:** Python 3.11+, Pydantic 2, stdlib `zipfile`/`sqlite3`/`hashlib`, Beautiful Soup 4, defusedxml, pypdf, pytest, Ruff, strict mypy.

## Global constraints

- Keep `forge`, MCP SDKs, model providers, outputs, and UI code out of this phase.
- EPUB order comes from the package spine; navigation metadata supplies titles when available.
- Reject encrypted EPUB/PDF, ZIP traversal, archive symlinks, nested archives, excessive members, expanded size, and compression ratio before reading book content.
- PDF with no meaningful text layer returns `OCR_REQUIRED`; the core never downloads or silently invokes OCR.
- File size, archive, and page limits are enforced before expensive work.
- Hash the source before and after parsing; a mismatch returns `SOURCE_CHANGED` and persists nothing.
- Managed imports are atomic and transactional. Reference-mode books retain a resolved source path plus fingerprint and fail clearly after source changes or disappears.
- With `library.enabled: false`, managed file import is unavailable but external snapshot upsert still works.
- Public errors never expose source text or private paths.
- Tests create original synthetic EPUB/PDF files at runtime; no copyrighted book fixture enters the repository.
- Adapted invisible-text sanitization from `book-to-skill` retains the upstream copyright/MIT notice and is recorded in `THIRD_PARTY_NOTICES.md`.

## Locked public interfaces

Add these frozen Pydantic contracts in `contracts/ingestion.py` and re-export them from `contracts/__init__.py`:

```python
class BookFormat(StrEnum):
    EPUB = "epub"
    PDF = "pdf"

class ImportMode(StrEnum):
    COPY = "copy"
    REFERENCE = "reference"

class PdfProfile(StrEnum):
    TEXT = "text"
    TECHNICAL = "technical"

class ExtractedBook(ContractModel):
    format: BookFormat
    metadata: BookMetadata
    chapters: tuple[ChapterContent, ...]
    source_fingerprint: str
    pdf_profile: PdfProfile | None = None

class ImportedBook(ContractModel):
    book: BookRef
    metadata: BookMetadata
    format: BookFormat
    import_mode: ImportMode
    source_fingerprint: str

class StoredBook(ContractModel):
    book: BookRef
    metadata: BookMetadata
    format: BookFormat | None = None
    import_mode: ImportMode | None = None
    source_fingerprint: str
    source_available: bool
    created_at: datetime
    updated_at: datetime
```

Internal extractor protocol:

```python
class BookExtractor(Protocol):
    def extract(self, source: Path, fingerprint: str) -> ExtractedBook: ...
```

Application service:

```python
class BookLibrary:
    def import_book(self, source: Path, mode: ImportMode | None = None) -> ImportedBook: ...
    def list_books(self) -> tuple[StoredBook, ...]: ...
    def get_book(self, book: BookRef) -> StoredBook: ...
    def get_chapter(self, book: BookRef, index: int) -> ChapterContent: ...
    def upsert_external_book(self, identity: ExternalIdentity, metadata: BookMetadata) -> BookRef: ...
    def upsert_chapter_snapshot(self, snapshot: ChapterSnapshot) -> BookRef: ...
```

Default extraction limits are internal frozen values: 512 MiB source, 5,000 PDF pages, 10,000 ZIP members, 1 GiB total expanded ZIP bytes, 128 MiB per member, and 100:1 compression ratio. Tests can inject smaller limits.

---

### Task 1: Add ingestion contracts, source validation, and sanitization

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/cove_book_forge/contracts/ingestion.py`
- Modify: `src/cove_book_forge/contracts/__init__.py`
- Create: `src/cove_book_forge/extractors/__init__.py`
- Create: `src/cove_book_forge/extractors/base.py`
- Create: `src/cove_book_forge/extractors/security.py`
- Create: `src/cove_book_forge/extractors/sanitize.py`
- Modify: `THIRD_PARTY_NOTICES.md`
- Create: `tests/test_ingestion_contracts.py`
- Create: `tests/test_extractor_security.py`
- Create: `tests/test_sanitize.py`

- [ ] Add failing tests for strict/frozen ingestion contracts, lowercase SHA-256 validation, magic-byte format detection, regular-file/size checks, pre/post fingerprint change detection helpers, and removal of invisible/bidirectional payload characters without deleting ordinary Chinese, Arabic, or Hebrew text.
- [ ] Add bounded dependencies: `beautifulsoup4>=4.13,<5`, `defusedxml>=0.7,<1`, and `pypdf>=5,<7`; refresh `uv.lock`.
- [ ] Implement `ExtractionLimits`, a streaming SHA-256 helper, source snapshot validation, `%PDF-`/EPUB magic detection, and `BookExtractor`/registry dispatch. Unsupported extensions or mismatched magic return `UNSUPPORTED_FORMAT`; absent/non-regular/oversized sources map to the existing safe error codes.
- [ ] Adapt the upstream sanitizer into `extractors/sanitize.py`, preserve the 2025 Virgilio Jr. copyright and MIT notice in the file, and describe the concrete reuse in `THIRD_PARTY_NOTICES.md`.
- [ ] Run:

```bash
uv run --no-sync pytest tests/test_ingestion_contracts.py tests/test_extractor_security.py tests/test_sanitize.py -v
uv run --no-sync ruff check src/cove_book_forge tests
uv run --no-sync mypy src/cove_book_forge
```

- [ ] Commit: `feat: add secure ingestion boundaries`

### Task 2: Parse EPUB safely in spine order

**Files:**

- Create: `src/cove_book_forge/extractors/epub.py`
- Create: `src/cove_book_forge/extractors/xhtml.py`
- Create: `tests/fixtures.py`
- Create: `tests/test_epub_extractor.py`

- [ ] Add failing runtime-fixture tests for a standard EPUB, nested OPF path, spine order differing from filename order, navigation titles, missing navigation, Unicode metadata, paragraphs/headings/lists/code/table/footnote readability, and empty content failure.
- [ ] Add hostile archive tests for absolute/parent paths, backslashes, encrypted entries, Unix symlink entries, nested archives, too many files, oversized members/total expansion, excessive compression ratio, malformed container/OPF, and missing spine documents.
- [ ] Implement a ZIP preflight that inspects every `ZipInfo` before parsing any XHTML. Resolve all archive references with POSIX semantics and require containment in the archive namespace.
- [ ] Parse `META-INF/container.xml`, OPF metadata/manifest/spine, and EPUB 3 nav or EPUB 2 NCX with `defusedxml`. Use spine order as authoritative; title preference is nav/NCX label, document heading, then a deterministic `Chapter N` fallback.
- [ ] Convert XHTML to compact readable Markdown-like text with Beautiful Soup, preserving headings, paragraphs, lists, fenced code, tables, blockquotes, and local footnote labels while removing scripts/styles/forms and sanitizing invisible controls.
- [ ] Return one `ChapterContent` per readable spine item with `epub:<member>` as `source_locator`; skip non-linear navigation documents and fail if no readable chapter remains.
- [ ] Run focused tests, Ruff, and strict mypy, then commit: `feat: parse epub books safely`.

### Task 3: Parse text-layer PDF and classify OCR-required files

**Files:**

- Create: `src/cove_book_forge/extractors/pdf.py`
- Modify: `tests/fixtures.py`
- Create: `tests/test_pdf_extractor.py`

- [ ] Add failing synthetic-PDF tests for metadata, text extraction, page order, repeated edge boilerplate, encrypted PDF, corrupt PDF, page limit, blank/scanned PDF, and a technical-looking page.
- [ ] Implement pypdf parsing with early encryption/page-count checks and per-page extraction. Sanitize extracted text and remove only repeated page-edge headers, footers, and unambiguous page-number lines; never delete matching mid-page headings.
- [ ] Use outline destinations as chapter boundaries when valid. Otherwise group readable pages into deterministic bounded ranges so the full book remains addressable without pretending every page is a semantic chapter. Record `pdf:pages:<start>-<end>` locators.
- [ ] Mark technical-looking content as `PdfProfile.TECHNICAL` using documented deterministic structure signals; otherwise use `TEXT`. Keep a layout-aware extractor protocol injectable for a later optional extra, with no automatic dependency download.
- [ ] If the document has no meaningful text layer, raise `OCR_REQUIRED`; malformed extraction raises `EXTRACTION_FAILED`. No empty `ExtractedBook` may be returned.
- [ ] Run focused tests, Ruff, and strict mypy, then commit: `feat: parse text layer pdf books`.

### Task 4: Persist managed books transactionally in SQLite

**Files:**

- Create: `src/cove_book_forge/library/__init__.py`
- Create: `src/cove_book_forge/library/database.py`
- Create: `src/cove_book_forge/library/repository.py`
- Create: `src/cove_book_forge/library/service.py`
- Create: `tests/test_library_database.py`
- Create: `tests/test_managed_library.py`

- [ ] Add failing tests for idempotent schema migration, foreign keys, duplicate fingerprint import, ordered chapter round-trip, list/get/not-found behavior, and rollback after an injected write failure.
- [ ] Create versioned SQLite schema for `books`, `chapters`, `external_sources`, and `chapter_snapshots`. Use foreign keys, unique external identity, unique `(book_id, chapter_index)`, UTC timestamps, and explicit transactions.
- [ ] Implement `LibraryRepository` with typed row mapping and no SQL exposed above the repository boundary.
- [ ] Implement `BookLibrary.import_book`: reject when the managed library is disabled; select the extractor; validate and hash before parsing; revalidate/hash after parsing; stage-and-atomically-copy originals for `COPY`; persist only the resolved path for `REFERENCE`; then insert book and chapters in one transaction.
- [ ] Default import mode follows `library.copy_imports`. Store managed originals beneath `<data_dir>/books/<book_id>/source.<ext>` and the SQLite file at `<data_dir>/library.sqlite3`; never derive paths from book metadata.
- [ ] On reads, report `source_available=False` for missing/changed reference files without deleting normalized chapters. Reject source reuse if a parse-time fingerprint changes.
- [ ] Run focused tests, Ruff, and strict mypy, then commit: `feat: add optional managed book library`.

### Task 5: Upsert external books and stable chapter snapshots

**Files:**

- Modify: `src/cove_book_forge/library/repository.py`
- Modify: `src/cove_book_forge/library/service.py`
- Modify: `src/cove_book_forge/library/__init__.py`
- Create: `tests/test_external_snapshots.py`

- [ ] Add failing tests showing that external upsert works with `library.enabled: false`, stable `(source_system, external_book_id)` returns the same internal `book_id`, repeated chapter index replaces instead of duplicates, stable highlight/note/annotation/reflection IDs remain in the normalized snapshot JSON, and total chapter metadata can grow.
- [ ] Implement `upsert_external_book` and transactional `upsert_chapter_snapshot`. Store the complete validated snapshot JSON plus a canonical SHA-256 content fingerprint, and mirror its normalized chapter into `chapters` for the shared `get_chapter` path.
- [ ] Keep the external system authoritative: never infer missing chapters or overwrite other chapter indices, and return `EXTERNAL_BOOK_INCOMPLETE` when a requested chapter has not been supplied.
- [ ] Ensure managed imports and external identities can coexist without identity collisions or private Cove fields.
- [ ] Run focused tests, Ruff, and strict mypy, then commit: `feat: persist external chapter snapshots`.

### Task 6: Integrate, document, and verify the phase

**Files:**

- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `src/cove_book_forge/doctor.py`
- Modify: `tests/test_cli_doctor.py`
- Create: `tests/test_ingestion_integration.py`

- [ ] Add an end-to-end test that imports a synthetic EPUB in copy mode, restarts the repository, lists it, and reads chapters; add a reference-mode PDF test that detects later source loss/change; add a library-disabled external snapshot round-trip.
- [ ] Extend doctor output with read-only checks for SQLite/data-directory readiness and EPUB/PDF dependency availability. It must not create directories or databases.
- [ ] Document supported formats, copy/reference semantics, OCR behavior, security limits, optional library behavior, and the external snapshot integration boundary. Keep the existing `book-to-skill` acknowledgement visible.
- [ ] Update the changelog with EPUB/PDF ingestion and optional library support; do not claim MCP transport, forging, or OCR is implemented.
- [ ] Run the complete quality gate:

```bash
uv lock --check
uv run --no-sync pytest --cov=cove_book_forge --cov-report=term-missing
uv run --no-sync ruff check .
uv run --no-sync mypy src/cove_book_forge
uv build --clear
uv run --no-sync python scripts/verify_distribution.py dist
git diff --check
git status --short
```

- [ ] Commit: `docs: document secure book ingestion`

## Definition of done

- Standard EPUB and text-layer PDF import into the optional local library and survive process restart.
- EPUB reading order follows the spine and preserves useful readable structure.
- Scanned/image-only PDF deterministically returns `OCR_REQUIRED`; encrypted files return `ENCRYPTED_DOCUMENT`.
- Hostile EPUB archives are rejected before extraction and leave no partial state.
- External systems can upsert normalized chapters while managed file import is disabled.
- Source changes during or after reference import are visible and never silently replace content.
- All new public models are strict/frozen, all errors use the existing closed safe representation, and no private content appears in errors/logs.
- Full tests, Ruff, strict mypy, build, distribution hygiene, attribution tests, and documentation checks pass.
