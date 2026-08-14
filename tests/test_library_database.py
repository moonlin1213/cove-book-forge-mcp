import sqlite3
from pathlib import Path

import pytest

from cove_book_forge.errors import ForgeErrorCode, ForgeException
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

    assert version == 1
    assert {"books", "chapters", "external_sources", "chapter_snapshots"} <= tables
    assert book_columns["format"] == 0
    assert book_columns["import_mode"] == 0
    assert book_columns["source_fingerprint"] == 0
    assert book_columns["managed_source_path"] == 0
    assert book_columns["reference_source_path"] == 0


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
