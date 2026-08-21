from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from cove_book_forge.analysis import ChapterAnalyzer
from cove_book_forge.analysis.chunks import split_chapter_content
from cove_book_forge.analysis.fingerprint import chapter_input_fingerprint
from cove_book_forge.config import AppConfig, library_data_path
from cove_book_forge.contracts import (
    BookRef,
    ChapterSnapshot,
    CostEstimate,
    ForgeAccepted,
    ForgeJob,
    ForgeJobControl,
    ForgeJobStatus,
    ForgePlan,
    ForgeTarget,
)
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.forge.store import ForgeStore
from cove_book_forge.library import BookLibrary
from cove_book_forge.outputs import AgentSkillOutput
from cove_book_forge.providers import ModelProvider


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class WholeBookForge:
    """Persistent, restartable controller for complete-book Agent Skill generation."""

    def __init__(
        self,
        config: AppConfig,
        library: BookLibrary,
        provider: ModelProvider,
        *,
        state_path: Path | None = None,
    ) -> None:
        self._config = config
        self._library = library
        self._provider = provider
        self._store = ForgeStore(state_path or library_data_path(config) / "forge.sqlite3")
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def plan_book_to_skill(
        self,
        *,
        book_id: str | None = None,
        snapshots: tuple[ChapterSnapshot, ...] | None = None,
        now: datetime | None = None,
    ) -> ForgePlan:
        chapters = self._resolve_snapshots(book_id=book_id, snapshots=snapshots)
        fingerprints = tuple(
            chapter_input_fingerprint(item, self._config.analysis, self._config.model)
            for item in chapters
        )
        pending: list[int] = []
        input_tokens = 0
        output_tokens = 0
        model_calls = 0
        for snapshot, fingerprint in zip(chapters, fingerprints, strict=True):
            cached = self._library.load_chapter_analysis(
                snapshot.source_system,
                snapshot.external_book_id,
                snapshot.chapter.index,
                fingerprint,
            )
            if cached is not None:
                continue
            pending.append(snapshot.chapter.index)
            chunks = split_chapter_content(
                snapshot.chapter.content, self._config.analysis.max_chunk_characters
            )
            calls = 1 if len(chunks) == 1 else len(chunks) + 1
            model_calls += calls
            input_tokens += math.ceil(len(snapshot.chapter.content) / 4)
            output_tokens += calls * self._config.model.default_max_output_tokens

        created = now or datetime.now(UTC)
        identity = (
            book_id or f"external-{_canonical_digest(chapters[0].external_identity.model_dump())}"
        )
        fingerprint_payload = {
            "chapters": fingerprints,
            "provider": self._config.model.provider,
            "model": self._config.model.model,
            "prompt": self._config.analysis.prompt_template_version,
            "generator": self._config.analysis.generator_version,
            "output": self._config.outputs.skills.model_dump(mode="json"),
        }
        plan = ForgePlan(
            plan_id=uuid4().hex,
            book_id=identity,
            book_fingerprint=_canonical_digest(fingerprint_payload),
            target=ForgeTarget.SKILL,
            total_chapters=len(chapters),
            processed_chapters=len(chapters) - len(pending),
            pending_chapters=tuple(pending),
            provider=self._config.model.provider,
            model=self._config.model.model,
            generator_version=self._config.analysis.generator_version,
            prompt_version=self._config.analysis.prompt_template_version,
            estimate=CostEstimate(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model_calls=model_calls,
            ),
            created_at=created,
            expires_at=created + timedelta(minutes=self._config.full_book_forge.plan_ttl_minutes),
        )
        self._store.save_plan(plan, chapters)
        return plan

    def forge_book_to_skill(
        self,
        plan_id: str,
        *,
        confirmed: bool,
        idempotency_key: str,
    ) -> ForgeAccepted:
        if self._config.full_book_forge.require_preflight_confirmation and not confirmed:
            raise ForgeException(
                ForgeErrorCode.CONFIRMATION_REQUIRED, "Explicit confirmation is required."
            )
        if not idempotency_key or len(idempotency_key) > 200:
            raise ForgeException(ForgeErrorCode.CONFIG_INVALID, "Idempotency key is invalid.")
        plan, _ = self._store.load_plan(plan_id)
        if plan.is_expired(datetime.now(UTC)):
            raise ForgeException(ForgeErrorCode.PLAN_EXPIRED, "Forge plan expired.")
        job = ForgeJob(
            job_id=uuid4().hex,
            book_id=plan.book_id,
            target=ForgeTarget.SKILL,
            total_chapters=plan.total_chapters,
        )
        actual = self._store.create_job(job, plan_id, idempotency_key)
        if actual.status not in {ForgeJobStatus.COMPLETED, ForgeJobStatus.CANCELLED}:
            self._start(actual.job_id)
        return ForgeAccepted(job_id=actual.job_id, status=actual.status, target=actual.target)

    def get_forge_job(self, job_id: str) -> ForgeJob:
        return self._store.load_job(job_id)

    def control_forge_job(self, job_id: str, control: ForgeJobControl) -> ForgeJob:
        job = self._store.load_job(job_id)
        if job.status in {ForgeJobStatus.COMPLETED, ForgeJobStatus.CANCELLED}:
            return job
        if control is ForgeJobControl.PAUSE:
            self._store.set_control(job_id, control.value)
            if job.status in {
                ForgeJobStatus.QUEUED,
                ForgeJobStatus.INTERRUPTED,
                ForgeJobStatus.FAILED,
            }:
                job = self._updated(job, status=ForgeJobStatus.PAUSED, current_chapter=None)
                self._store.save_job(job)
        elif control is ForgeJobControl.CANCEL:
            self._store.set_control(job_id, control.value)
            if job.status in {
                ForgeJobStatus.QUEUED,
                ForgeJobStatus.PAUSED,
                ForgeJobStatus.INTERRUPTED,
                ForgeJobStatus.FAILED,
            }:
                job = self._updated(job, status=ForgeJobStatus.CANCELLED, current_chapter=None)
                self._store.save_job(job)
        else:
            self._store.set_control(job_id, None)
            if job.status in {
                ForgeJobStatus.PAUSED,
                ForgeJobStatus.INTERRUPTED,
                ForgeJobStatus.FAILED,
            }:
                job = self._updated(
                    job, status=ForgeJobStatus.QUEUED, current_chapter=None, error=None
                )
                self._store.save_job(job)
                self._start(job_id)
        return self._store.load_job(job_id)

    async def wait_for_job(self, job_id: str) -> ForgeJob:
        task = self._tasks.get(job_id)
        if task is not None:
            await task
        return self._store.load_job(job_id)

    def _start(self, job_id: str) -> None:
        existing = self._tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        try:
            self._tasks[job_id] = asyncio.get_running_loop().create_task(self._run(job_id))
        except RuntimeError as exc:
            raise ForgeException(
                ForgeErrorCode.CONFIG_INVALID, "Whole-book forge requires an async runtime."
            ) from exc

    async def _run(self, job_id: str) -> None:
        plan_id = self._store.job_plan_id(job_id)
        _, snapshots = self._store.load_plan(plan_id)
        done = self._store.checkpoints(job_id)
        output = AgentSkillOutput(self._config.outputs.skills)
        analyzer = ChapterAnalyzer(
            self._provider, self._library, self._config.analysis, self._config.model
        )
        job = self._store.load_job(job_id)
        try:
            for snapshot in snapshots:
                if snapshot.chapter.index in done:
                    continue
                boundary = self._store.control(job_id)
                if boundary == ForgeJobControl.CANCEL.value:
                    self._store.save_job(
                        self._updated(job, status=ForgeJobStatus.CANCELLED, current_chapter=None)
                    )
                    return
                if boundary == ForgeJobControl.PAUSE.value:
                    self._store.save_job(
                        self._updated(job, status=ForgeJobStatus.PAUSED, current_chapter=None)
                    )
                    return
                job = self._updated(
                    job,
                    status=ForgeJobStatus.ANALYZING,
                    current_chapter=snapshot.chapter.index,
                    failed_chapters=(),
                    error=None,
                )
                self._store.save_job(job)
                analyzed = await analyzer.analyze(snapshot)
                job = self._updated(job, status=ForgeJobStatus.PUBLISHING)
                self._store.save_job(job)
                output.publish(snapshot, analyzed)
                self._store.checkpoint(job_id, snapshot.chapter.index)
                job = self._updated(
                    job,
                    processed_chapters=len(self._store.checkpoints(job_id)),
                    current_chapter=None,
                )
                self._store.save_job(job)
            self._store.save_job(
                self._updated(job, status=ForgeJobStatus.COMPLETED, current_chapter=None),
                control=None,
            )
        except ForgeException as exc:
            current = job.current_chapter
            failed = () if current is None else (current,)
            self._store.save_job(
                self._updated(
                    job,
                    status=ForgeJobStatus.FAILED,
                    failed_chapters=failed,
                    current_chapter=None,
                    error=exc.as_detail(),
                )
            )
        except Exception:
            safe = ForgeException(
                ForgeErrorCode.JOB_INTERRUPTED,
                "Whole-book forge was interrupted.",
                retryable=True,
            )
            self._store.save_job(
                self._updated(
                    job,
                    status=ForgeJobStatus.FAILED,
                    current_chapter=None,
                    error=safe.as_detail(),
                )
            )

    def _resolve_snapshots(
        self,
        *,
        book_id: str | None,
        snapshots: tuple[ChapterSnapshot, ...] | None,
    ) -> tuple[ChapterSnapshot, ...]:
        if (book_id is None) == (snapshots is None):
            raise ForgeException(
                ForgeErrorCode.CONFIG_INVALID,
                "Supply exactly one managed book id or complete snapshot sequence.",
            )
        if snapshots is not None:
            if not snapshots:
                raise ForgeException(
                    ForgeErrorCode.EXTERNAL_BOOK_INCOMPLETE, "External book is incomplete."
                )
            first = snapshots[0]
            ordered = tuple(sorted(snapshots, key=lambda item: item.chapter.index))
            expected = tuple(range(first.book.total_chapters))
            if (
                tuple(item.chapter.index for item in ordered) != expected
                or any(item.external_identity != first.external_identity for item in ordered)
                or any(item.book != first.book for item in ordered)
            ):
                raise ForgeException(
                    ForgeErrorCode.EXTERNAL_BOOK_INCOMPLETE, "External book is incomplete."
                )
            return ordered
        assert book_id is not None
        stored = self._library.get_book(BookRef(book_id=book_id))
        return tuple(
            ChapterSnapshot(
                source_system="cove-library",
                external_book_id=book_id,
                book=stored.metadata,
                chapter=self._library.get_chapter(stored.book, index),
            )
            for index in range(stored.metadata.total_chapters)
        )

    @staticmethod
    def _updated(job: ForgeJob, **changes: object) -> ForgeJob:
        return job.model_copy(update={**changes, "updated_at": datetime.now(UTC)})
