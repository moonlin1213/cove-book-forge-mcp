import json
from pathlib import Path

import pytest

from cove_book_forge.config import AppConfig
from cove_book_forge.contracts import ChapterAnalysis
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.library import BookLibrary


def _library(data_dir: Path, *, enabled: bool = False) -> BookLibrary:
    config = AppConfig.model_validate(
        {
            "library": {"enabled": enabled, "data_dir": data_dir},
            "model": {"provider": "test", "model": "test"},
        }
    )
    library = BookLibrary(config)
    library.initialize()
    return library


def _analysis(core_idea: str = "A validated insight.") -> ChapterAnalysis:
    return ChapterAnalysis(core_idea=core_idea, topic_tags=("cache",))


def test_disabled_library_persists_and_reuses_a_validated_analysis_by_full_key(
    tmp_path: Path,
) -> None:
    """Removing any identity/key field from the lookup must make this fail."""
    library = _library(tmp_path / "cache", enabled=False)
    fingerprint = "a" * 64
    analysis = _analysis()

    library.store_chapter_analysis("reader", "book-1", 2, fingerprint, analysis)

    assert library.load_chapter_analysis("reader", "book-1", 2, fingerprint) == analysis
    assert library.load_chapter_analysis("reader", "book-1", 3, fingerprint) is None
    assert library.load_chapter_analysis("other-reader", "book-1", 2, fingerprint) is None
    assert library.load_chapter_analysis("reader", "other-book", 2, fingerprint) is None
    assert library.load_chapter_analysis("reader", "book-1", 2, "b" * 64) is None


def test_changed_fingerprint_replaces_one_chapter_cache_entry(tmp_path: Path) -> None:
    """Dropping the primary-key upsert must leave stale chapter cache data behind."""
    library = _library(tmp_path / "cache")
    old_fingerprint = "a" * 64
    new_fingerprint = "b" * 64
    replacement = _analysis("Replacement analysis.")

    library.store_chapter_analysis("reader", "book-1", 0, old_fingerprint, _analysis())
    library.store_chapter_analysis("reader", "book-1", 0, new_fingerprint, replacement)

    assert library.load_chapter_analysis("reader", "book-1", 0, old_fingerprint) is None
    assert library.load_chapter_analysis("reader", "book-1", 0, new_fingerprint) == replacement


def test_cache_storage_is_canonical_and_corruption_fails_closed_without_content(
    tmp_path: Path,
) -> None:
    """Removing strict stored-JSON validation must expose malformed rows as valid analyses."""
    library = _library(tmp_path / "cache")
    fingerprint = "a" * 64
    library.store_chapter_analysis("reader", "book-1", 0, fingerprint, _analysis())

    database = library._repository._database  # noqa: SLF001 - corrupt persistent fixture
    with database.transaction() as connection:
        stored = connection.execute("SELECT analysis_json FROM chapter_analyses").fetchone()[0]
        assert stored == json.dumps(
            _analysis().model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        connection.execute(
            "UPDATE chapter_analyses SET analysis_json = ?",
            ('{"core_idea":"","private":"Private chapter content must not leak"}',),
        )

    with pytest.raises(ForgeException) as exc_info:
        library.load_chapter_analysis("reader", "book-1", 0, fingerprint)

    assert exc_info.value.code is ForgeErrorCode.MODEL_OUTPUT_INVALID
    assert "core_idea" not in str(exc_info.value)
    assert "reader" not in str(exc_info.value)
    assert "Private chapter content" not in str(exc_info.value)


def test_failed_cache_upsert_rolls_back_without_exposing_database_details(tmp_path: Path) -> None:
    """Removing the transaction boundary must replace a working cache entry after a failed write."""
    library = _library(tmp_path / "cache")
    old_fingerprint = "a" * 64
    old_analysis = _analysis()
    library.store_chapter_analysis("reader", "book-1", 0, old_fingerprint, old_analysis)

    database = library._repository._database  # noqa: SLF001 - database failure fixture
    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_cache_update
            BEFORE UPDATE ON chapter_analyses
            BEGIN
                SELECT RAISE(ABORT, 'private cache SQL detail');
            END
            """
        )

    with pytest.raises(ForgeException) as exc_info:
        library.store_chapter_analysis(
            "reader",
            "book-1",
            0,
            "b" * 64,
            _analysis("A replacement that must not be partially stored."),
        )

    assert exc_info.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert "private cache SQL detail" not in str(exc_info.value)
    assert library.load_chapter_analysis("reader", "book-1", 0, old_fingerprint) == old_analysis


@pytest.mark.parametrize("fingerprint", ["A" * 64, "a" * 63, "g" * 64])
def test_cache_rejects_noncanonical_fingerprints(tmp_path: Path, fingerprint: str) -> None:
    """Weakening lowercase SHA-256 validation must permit an invalid cache namespace."""
    library = _library(tmp_path / "cache")

    with pytest.raises(ForgeException) as exc_info:
        library.store_chapter_analysis("reader", "book-1", 0, fingerprint, _analysis())

    assert exc_info.value.code is ForgeErrorCode.CONFIG_INVALID
