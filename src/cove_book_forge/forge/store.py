from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from cove_book_forge.contracts import ChapterSnapshot, ForgeJob, ForgeJobStatus, ForgePlan
from cove_book_forge.errors import ForgeErrorCode, ForgeException

_ACTIVE = tuple(
    status.value
    for status in ForgeJobStatus
    if status not in {ForgeJobStatus.COMPLETED, ForgeJobStatus.CANCELLED}
)
_RUNNING = tuple(
    status.value
    for status in (
        ForgeJobStatus.QUEUED,
        ForgeJobStatus.PLANNING,
        ForgeJobStatus.PARSING,
        ForgeJobStatus.ANALYZING,
        ForgeJobStatus.SYNTHESIZING,
        ForgeJobStatus.VALIDATING,
        ForgeJobStatus.PUBLISHING,
    )
)


def _safe_storage_error() -> ForgeException:
    return ForgeException(
        ForgeErrorCode.OUTPUT_PERMISSION_DENIED,
        "Forge state storage is unavailable.",
    )


class ForgeStore:
    """Small private SQLite journal for plans, jobs, and chapter checkpoints."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as connection, connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA foreign_keys=ON;
                    CREATE TABLE IF NOT EXISTS forge_plans (
                        plan_id TEXT PRIMARY KEY,
                        plan_json TEXT NOT NULL,
                        snapshots_json TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS forge_jobs (
                        job_id TEXT PRIMARY KEY,
                        plan_id TEXT NOT NULL REFERENCES forge_plans(plan_id),
                        book_id TEXT NOT NULL,
                        target TEXT NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        job_json TEXT NOT NULL,
                        requested_control TEXT,
                        UNIQUE(book_id, target, idempotency_key)
                    );
                    CREATE TABLE IF NOT EXISTS forge_checkpoints (
                        job_id TEXT NOT NULL REFERENCES forge_jobs(job_id) ON DELETE CASCADE,
                        chapter_index INTEGER NOT NULL,
                        PRIMARY KEY(job_id, chapter_index)
                    );
                    """
                )
                placeholders = ",".join("?" for _ in _RUNNING)
                rows = connection.execute(
                    f"SELECT job_id, job_json FROM forge_jobs "
                    f"WHERE json_extract(job_json, '$.status') IN ({placeholders})",
                    _RUNNING,
                ).fetchall()
                for row in rows:
                    job = ForgeJob.model_validate_json(row["job_json"])
                    interrupted = job.model_copy(
                        update={
                            "status": ForgeJobStatus.INTERRUPTED,
                            "current_chapter": None,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    connection.execute(
                        "UPDATE forge_jobs SET job_json = ? WHERE job_id = ?",
                        (interrupted.model_dump_json(), job.job_id),
                    )
        except (OSError, sqlite3.Error, ValidationError):
            raise _safe_storage_error() from None

    def save_plan(self, plan: ForgePlan, snapshots: tuple[ChapterSnapshot, ...]) -> None:
        try:
            snapshots_json = json.dumps(
                [item.model_dump(mode="json") for item in snapshots],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO forge_plans(plan_id, plan_json, snapshots_json) VALUES (?, ?, ?)",
                    (plan.plan_id, plan.model_dump_json(), snapshots_json),
                )
        except (TypeError, ValueError, sqlite3.Error):
            raise _safe_storage_error() from None

    def load_plan(self, plan_id: str) -> tuple[ForgePlan, tuple[ChapterSnapshot, ...]]:
        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    "SELECT plan_json, snapshots_json FROM forge_plans WHERE plan_id = ?",
                    (plan_id,),
                ).fetchone()
            if row is None:
                raise ForgeException(ForgeErrorCode.SOURCE_NOT_FOUND, "Forge plan was not found.")
            plan = ForgePlan.model_validate_json(row["plan_json"])
            snapshots = tuple(
                ChapterSnapshot.model_validate(item) for item in json.loads(row["snapshots_json"])
            )
            return plan, snapshots
        except ForgeException:
            raise
        except (sqlite3.Error, ValidationError, json.JSONDecodeError, TypeError):
            raise _safe_storage_error() from None

    def create_job(self, job: ForgeJob, plan_id: str, idempotency_key: str) -> ForgeJob:
        try:
            with closing(self._connect()) as connection, connection:
                existing = connection.execute(
                    "SELECT job_json FROM forge_jobs "
                    "WHERE book_id = ? AND target = ? AND idempotency_key = ?",
                    (job.book_id, job.target.value, idempotency_key),
                ).fetchone()
                if existing is not None:
                    return ForgeJob.model_validate_json(existing["job_json"])
                placeholders = ",".join("?" for _ in _ACTIVE)
                active = connection.execute(
                    f"SELECT 1 FROM forge_jobs WHERE book_id = ? AND target = ? "
                    f"AND json_extract(job_json, '$.status') IN ({placeholders}) LIMIT 1",
                    (job.book_id, job.target.value, *_ACTIVE),
                ).fetchone()
                if active is not None:
                    raise ForgeException(
                        ForgeErrorCode.JOB_CONFLICT,
                        "Another job already controls this book and target.",
                    )
                connection.execute(
                    "INSERT INTO forge_jobs(job_id, plan_id, book_id, target, "
                    "idempotency_key, job_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        job.job_id,
                        plan_id,
                        job.book_id,
                        job.target.value,
                        idempotency_key,
                        job.model_dump_json(),
                    ),
                )
            return job
        except ForgeException:
            raise
        except (sqlite3.Error, ValidationError):
            raise _safe_storage_error() from None

    def load_job(self, job_id: str) -> ForgeJob:
        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    "SELECT job_json FROM forge_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            if row is None:
                raise ForgeException(ForgeErrorCode.SOURCE_NOT_FOUND, "Forge job was not found.")
            return ForgeJob.model_validate_json(row["job_json"])
        except ForgeException:
            raise
        except (sqlite3.Error, ValidationError):
            raise _safe_storage_error() from None

    def job_plan_id(self, job_id: str) -> str:
        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    "SELECT plan_id FROM forge_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            if row is None:
                raise ForgeException(ForgeErrorCode.SOURCE_NOT_FOUND, "Forge job was not found.")
            return str(row["plan_id"])
        except ForgeException:
            raise
        except sqlite3.Error:
            raise _safe_storage_error() from None

    def save_job(self, job: ForgeJob, *, control: str | None | object = ...) -> None:
        try:
            with closing(self._connect()) as connection, connection:
                if control is ...:
                    connection.execute(
                        "UPDATE forge_jobs SET job_json = ? WHERE job_id = ?",
                        (job.model_dump_json(), job.job_id),
                    )
                else:
                    connection.execute(
                        "UPDATE forge_jobs SET job_json = ?, requested_control = ? WHERE job_id = ?",
                        (job.model_dump_json(), control, job.job_id),
                    )
        except sqlite3.Error:
            raise _safe_storage_error() from None

    def set_control(self, job_id: str, control: str | None) -> None:
        try:
            with closing(self._connect()) as connection, connection:
                changed = connection.execute(
                    "UPDATE forge_jobs SET requested_control = ? WHERE job_id = ?",
                    (control, job_id),
                ).rowcount
            if changed != 1:
                raise ForgeException(ForgeErrorCode.SOURCE_NOT_FOUND, "Forge job was not found.")
        except ForgeException:
            raise
        except sqlite3.Error:
            raise _safe_storage_error() from None

    def control(self, job_id: str) -> str | None:
        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    "SELECT requested_control FROM forge_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
            if row is None:
                raise ForgeException(ForgeErrorCode.SOURCE_NOT_FOUND, "Forge job was not found.")
            value = row["requested_control"]
            return None if value is None else str(value)
        except ForgeException:
            raise
        except sqlite3.Error:
            raise _safe_storage_error() from None

    def checkpoint(self, job_id: str, chapter_index: int) -> None:
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "INSERT OR IGNORE INTO forge_checkpoints(job_id, chapter_index) VALUES (?, ?)",
                    (job_id, chapter_index),
                )
        except sqlite3.Error:
            raise _safe_storage_error() from None

    def checkpoints(self, job_id: str) -> frozenset[int]:
        try:
            with closing(self._connect()) as connection, connection:
                rows = connection.execute(
                    "SELECT chapter_index FROM forge_checkpoints WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
            return frozenset(int(row["chapter_index"]) for row in rows)
        except sqlite3.Error:
            raise _safe_storage_error() from None
