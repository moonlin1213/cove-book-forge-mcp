import sqlite3
from pathlib import Path

import pytest

from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.library import database as library_database
from cove_book_forge.library.database import LibraryDatabase


def _insert_minimal_book(connection: sqlite3.Connection, book_id: str = "book-1") -> None:
    connection.execute(
        """
        INSERT INTO books (
            book_id, title, author, language, total_chapters, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            book_id,
            "Synthetic book",
            "",
            "",
            0,
            "2026-08-14T00:00:00+00:00",
            "2026-08-14T00:00:00+00:00",
        ),
    )


def test_schema_migration_is_explicit_idempotent_and_forward_compatible(tmp_path: Path) -> None:
    database = LibraryDatabase(tmp_path / "library.sqlite3")

    database.initialize()
    database.initialize()

    with database.connect() as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        book_columns = {
            row[1]: row[3] for row in connection.execute("PRAGMA table_info(books)").fetchall()
        }

    assert version == 3
    assert {
        "books",
        "chapters",
        "external_sources",
        "chapter_snapshots",
        "chapter_analyses",
    } <= tables
    assert book_columns["format"] == 0
    assert book_columns["import_mode"] == 0
    assert book_columns["source_fingerprint"] == 0
    assert book_columns["managed_source_path"] == 0
    assert book_columns["reference_source_path"] == 0


def test_v3_database_reopens_without_rerunning_or_mutating_schema(tmp_path: Path) -> None:
    path = tmp_path / "library.sqlite3"
    first = LibraryDatabase(path)
    first.initialize()
    with first.transaction() as connection:
        _insert_minimal_book(connection)
    with first.connect() as connection:
        original_schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'chapter_snapshots'"
        ).fetchone()[0]

    reopened = LibraryDatabase(path)
    reopened.initialize()
    reopened.initialize()

    with reopened.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert (
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'chapter_snapshots'"
            ).fetchone()[0]
            == original_schema
        )
        assert connection.execute("SELECT book_id FROM books").fetchone()[0] == "book-1"


def test_v2_database_migrates_to_v3_without_changing_existing_data(tmp_path: Path) -> None:
    """Skipping the v2-to-v3 migration must leave this valid database cache-incomplete."""
    path = tmp_path / "library.sqlite3"
    with sqlite3.connect(path) as connection, connection:
        for statement in library_database._SCHEMA_V1:  # noqa: SLF001 - legacy fixture
            connection.execute(statement)
        connection.execute(
            "ALTER TABLE chapter_snapshots "
            "ADD COLUMN content_fingerprint TEXT NOT NULL DEFAULT ''"
        )
        connection.execute("PRAGMA user_version = 2")
        _insert_minimal_book(connection)

    database = LibraryDatabase(path)
    database.initialize()

    with database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT book_id FROM books").fetchone()[0] == "book-1"
        assert connection.execute("SELECT COUNT(*) FROM chapter_analyses").fetchone()[0] == 0


def test_every_database_connection_enables_foreign_keys(tmp_path: Path) -> None:
    database = LibraryDatabase(tmp_path / "library.sqlite3")
    database.initialize()

    with database.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO chapters (book_id, chapter_index, title, content, source_locator)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("missing", 0, "", "Readable.", ""),
            )


def test_transaction_rolls_back_and_chapter_indices_are_unique_per_book(tmp_path: Path) -> None:
    database = LibraryDatabase(tmp_path / "library.sqlite3")
    database.initialize()

    with (
        pytest.raises(RuntimeError, match="injected failure"),
        database.transaction() as connection,
    ):
        _insert_minimal_book(connection)
        raise RuntimeError("injected failure")

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0

    with database.transaction() as connection:
        _insert_minimal_book(connection)
        connection.execute(
            """
            INSERT INTO chapters (book_id, chapter_index, title, content, source_locator)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("book-1", 0, "Chapter", "Readable.", "page:1"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO chapters (book_id, chapter_index, title, content, source_locator)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("book-1", 0, "Duplicate", "Duplicate.", "page:2"),
            )


def test_database_initialization_failure_is_safe(tmp_path: Path) -> None:
    occupied_parent = tmp_path / "not-a-directory"
    occupied_parent.write_text("private", encoding="utf-8")
    database_path = occupied_parent / "secret-library.sqlite3"

    with pytest.raises(ForgeException) as exc_info:
        LibraryDatabase(database_path).initialize()

    assert exc_info.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert str(database_path) not in str(exc_info.value)
    assert "sqlite" not in str(exc_info.value).lower()


def test_failed_stable_connection_setup_closes_the_candidate_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SetupFailingConnection:
        row_factory: object | None = None
        closed = False

        def execute(self, statement: str) -> None:
            raise sqlite3.OperationalError(f"private PRAGMA failed: {statement}")

        def close(self) -> None:
            self.closed = True

    candidate = SetupFailingConnection()
    monkeypatch.setattr(library_database.sqlite3, "connect", lambda *args, **kwargs: candidate)

    with pytest.raises(ForgeException) as exc_info:
        LibraryDatabase(tmp_path / "private-library.sqlite3").initialize()

    assert candidate.closed is True
    assert exc_info.value.code is ForgeErrorCode.OUTPUT_PERMISSION_DENIED
    assert "PRAGMA" not in str(exc_info.value)
    assert str(tmp_path) not in str(exc_info.value)
