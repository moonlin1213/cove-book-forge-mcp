from __future__ import annotations

# mypy: disable-error-code="untyped-decorator"
import hashlib
import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ResourceError, ToolError
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette

from cove_book_forge.analysis import ChapterAnalyzer
from cove_book_forge.config import AppConfig
from cove_book_forge.contracts import (
    AnalyzedChapter,
    BookRef,
    ChapterContent,
    ChapterSnapshot,
    ForgeAccepted,
    ForgeJob,
    ForgeJobControl,
    ForgePlan,
    ImportedBook,
    ImportMode,
    ObsidianPublishResult,
    SkillPublishResult,
    StoredBook,
)
from cove_book_forge.doctor import DoctorCheck, run_doctor_config
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.forge import WholeBookForge
from cove_book_forge.library import BookLibrary, create_book_library
from cove_book_forge.outputs import AgentSkillOutput, ObsidianOutput
from cove_book_forge.outputs.skill_models import AgentSkillManifest
from cove_book_forge.outputs.skill_publisher import CanonicalSkillPublisher
from cove_book_forge.providers import ModelProvider, ProviderRegistry


class MCPResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DoctorToolResult(MCPResult):
    ok: bool
    checks: tuple[DoctorCheck, ...]


class BookListResult(MCPResult):
    books: tuple[StoredBook, ...]


class SkillSummary(MCPResult):
    book_key: str = Field(pattern=r"^[0-9a-f]{16}$")
    skill_slug: str
    book_title: str
    rendered_chapters: int = Field(ge=0)
    total_chapters: int = Field(ge=0)
    complete: bool

    @classmethod
    def from_manifest(cls, manifest: AgentSkillManifest) -> SkillSummary:
        return cls(
            book_key=manifest.book_key,
            skill_slug=manifest.skill_slug,
            book_title=manifest.book_title,
            rendered_chapters=len(manifest.chapters),
            total_chapters=manifest.total_chapters,
            complete=len(manifest.chapters) == manifest.total_chapters,
        )


class GeneratedSkillsResult(MCPResult):
    skills: tuple[SkillSummary, ...]


class BookSkillStatus(MCPResult):
    book_id: str
    skill: SkillSummary | None = None


@dataclass(frozen=True, slots=True)
class AppContext:
    config: AppConfig
    library: BookLibrary
    provider: ModelProvider
    forge: WholeBookForge

    @classmethod
    def create(
        cls,
        config: AppConfig,
        *,
        library: BookLibrary | None = None,
        provider: ModelProvider | None = None,
    ) -> AppContext:
        actual_library = library or create_book_library(config)
        actual_provider = provider or ProviderRegistry().create(config.model)
        return cls(
            config=config,
            library=actual_library,
            provider=actual_provider,
            forge=WholeBookForge(config, actual_library, actual_provider),
        )

    def snapshot(self, book_id: str, chapter_index: int) -> ChapterSnapshot:
        stored = self.library.get_book(BookRef(book_id=book_id))
        return ChapterSnapshot(
            source_system="cove-library",
            external_book_id=book_id,
            book=stored.metadata,
            chapter=self.library.get_chapter(stored.book, chapter_index),
        )

    def skill_manifests(self) -> tuple[AgentSkillManifest, ...]:
        return CanonicalSkillPublisher(self.config.outputs.skills).list_active_manifests()


def _safe_tool_error(exc: ForgeException) -> ToolError:
    return ToolError(exc.as_detail().model_dump_json())


def _safe_resource_error(exc: ForgeException) -> ResourceError:
    return ResourceError(exc.as_detail().model_dump_json())


def _json(model: BaseModel) -> str:
    return model.model_dump_json(by_alias=True)


def _book_key(book_id: str) -> str:
    payload = json.dumps(["cove-library", book_id], separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def create_mcp_server(
    context: AppContext,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> FastMCP:
    """Build the MCP server around an explicitly injected application context."""
    server = FastMCP(
        "Cove Book Forge",
        instructions="Local-first book analysis and complete Agent Skill forging.",
        host=host,
        port=port,
        json_response=True,
    )

    @server.tool(structured_output=True)
    def book_forge_doctor() -> DoctorToolResult:
        """Run network-free configuration and local storage diagnostics."""
        report = run_doctor_config(context.config)
        return DoctorToolResult(ok=report.ok, checks=report.checks)

    @server.tool(structured_output=True)
    def import_book(source_path: str, mode: ImportMode = ImportMode.COPY) -> ImportedBook:
        """Import a local EPUB or text-layer PDF into the managed library."""
        try:
            return context.library.import_book(Path(source_path), mode)
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.tool(structured_output=True)
    def list_books() -> BookListResult:
        """List locally stored books without returning chapter bodies or source paths."""
        try:
            return BookListResult(books=context.library.list_books())
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.tool(structured_output=True)
    def get_book(book_id: str) -> StoredBook:
        """Get one stored book's metadata and availability."""
        try:
            return context.library.get_book(BookRef(book_id=book_id))
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.tool(structured_output=True)
    def get_chapter(book_id: str, chapter_index: int) -> ChapterContent:
        """Read one normalized chapter from a stored book."""
        try:
            return context.library.get_chapter(BookRef(book_id=book_id), chapter_index)
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.tool(structured_output=True)
    async def analyze_chapter(book_id: str, chapter_index: int) -> AnalyzedChapter:
        """Analyze one stored chapter, reusing its persistent fingerprint cache."""
        try:
            snapshot = context.snapshot(book_id, chapter_index)
            return await ChapterAnalyzer(
                context.provider,
                context.library,
                context.config.analysis,
                context.config.model,
            ).analyze(snapshot)
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    async def _analyzed(
        book_id: str, chapter_index: int
    ) -> tuple[ChapterSnapshot, AnalyzedChapter]:
        snapshot = context.snapshot(book_id, chapter_index)
        analyzed = await ChapterAnalyzer(
            context.provider,
            context.library,
            context.config.analysis,
            context.config.model,
        ).analyze(snapshot)
        return snapshot, analyzed

    @server.tool(structured_output=True)
    async def forge_chapter_to_obsidian(book_id: str, chapter_index: int) -> ObsidianPublishResult:
        """Analyze and publish one chapter to the configured managed Obsidian vault."""
        try:
            snapshot, analyzed = await _analyzed(book_id, chapter_index)
            return ObsidianOutput(context.config.outputs.obsidian).publish(snapshot, analyzed)
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.tool(structured_output=True)
    async def forge_chapter_to_skill(book_id: str, chapter_index: int) -> SkillPublishResult:
        """Analyze and incrementally publish one chapter into its managed Agent Skill."""
        try:
            snapshot, analyzed = await _analyzed(book_id, chapter_index)
            return AgentSkillOutput(context.config.outputs.skills).publish(snapshot, analyzed)
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.tool(structured_output=True)
    def plan_book_to_skill(
        book_id: str | None = None,
        snapshots: tuple[ChapterSnapshot, ...] | None = None,
    ) -> ForgePlan:
        """Create a 30-minute, fingerprint-bound whole-book preflight plan."""
        try:
            return context.forge.plan_book_to_skill(book_id=book_id, snapshots=snapshots)
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.tool(structured_output=True)
    def forge_book_to_skill(plan_id: str, confirmed: bool, idempotency_key: str) -> ForgeAccepted:
        """Explicitly confirm and start one persistent complete-book Skill job."""
        try:
            return context.forge.forge_book_to_skill(
                plan_id, confirmed=confirmed, idempotency_key=idempotency_key
            )
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.tool(structured_output=True)
    def get_forge_job(job_id: str) -> ForgeJob:
        """Get persistent whole-book job progress without source text."""
        try:
            return context.forge.get_forge_job(job_id)
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.tool(structured_output=True)
    def control_forge_job(job_id: str, control: ForgeJobControl) -> ForgeJob:
        """Pause, resume/retry, or cancel a job at a chapter boundary."""
        try:
            return context.forge.control_forge_job(job_id, control)
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.tool(structured_output=True)
    def get_book_skill_status(book_id: str) -> BookSkillStatus:
        """Report generated Skill coverage for one managed library book."""
        try:
            context.library.get_book(BookRef(book_id=book_id))
            key = _book_key(book_id)
            manifest = next(
                (item for item in context.skill_manifests() if item.book_key == key), None
            )
            return BookSkillStatus(
                book_id=book_id,
                skill=None if manifest is None else SkillSummary.from_manifest(manifest),
            )
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.tool(structured_output=True)
    def list_generated_skills() -> GeneratedSkillsResult:
        """List validated active generated Skills and their chapter coverage."""
        try:
            return GeneratedSkillsResult(
                skills=tuple(SkillSummary.from_manifest(item) for item in context.skill_manifests())
            )
        except ForgeException as exc:
            raise _safe_tool_error(exc) from None

    @server.resource("cove-book-forge://books/{book_id}", mime_type="application/json")
    def book_resource(book_id: str) -> str:
        try:
            return _json(context.library.get_book(BookRef(book_id=book_id)))
        except ForgeException as exc:
            raise _safe_resource_error(exc) from None

    @server.resource(
        "cove-book-forge://books/{book_id}/chapters/{chapter_index}",
        mime_type="application/json",
    )
    def chapter_resource(book_id: str, chapter_index: int) -> str:
        try:
            return _json(context.library.get_chapter(BookRef(book_id=book_id), chapter_index))
        except ForgeException as exc:
            raise _safe_resource_error(exc) from None

    @server.resource("cove-book-forge://books/{book_id}/skill", mime_type="application/json")
    def book_skill_resource(book_id: str) -> str:
        try:
            context.library.get_book(BookRef(book_id=book_id))
            key = _book_key(book_id)
            manifest = next(
                (item for item in context.skill_manifests() if item.book_key == key), None
            )
            if manifest is None:
                raise ForgeException(ForgeErrorCode.SOURCE_NOT_FOUND, "Skill was not found.")
            return _json(manifest)
        except ForgeException as exc:
            raise _safe_resource_error(exc) from None

    @server.resource("cove-book-forge://jobs/{job_id}", mime_type="application/json")
    def job_resource(job_id: str) -> str:
        try:
            return _json(context.forge.get_forge_job(job_id))
        except ForgeException as exc:
            raise _safe_resource_error(exc) from None

    @server.resource("cove-book-forge://skills/{skill_slug}", mime_type="application/json")
    def skill_resource(skill_slug: str) -> str:
        try:
            manifest = CanonicalSkillPublisher(context.config.outputs.skills).current_manifest(
                skill_slug
            )
            if manifest is None:
                raise ForgeException(ForgeErrorCode.SOURCE_NOT_FOUND, "Skill was not found.")
            return _json(manifest)
        except ForgeException as exc:
            raise _safe_resource_error(exc) from None

    return server


def create_streamable_http_app(
    context: AppContext,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> Starlette:
    """Return a loopback-only Streamable HTTP ASGI application."""
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError
    except ValueError:
        raise ForgeException(
            ForgeErrorCode.CONFIG_INVALID,
            "Unauthenticated MCP HTTP is restricted to a loopback address.",
        ) from None
    return create_mcp_server(context, host=host, port=port).streamable_http_app()


MCPTransport = Literal["stdio", "http"]
