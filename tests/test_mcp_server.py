from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from fixtures import basic_epub
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import JsonValue

from cove_book_forge.config import AppConfig
from cove_book_forge.errors import ForgeErrorCode, ForgeException
from cove_book_forge.library import create_book_library
from cove_book_forge.mcp_server import AppContext, create_mcp_server, create_streamable_http_app
from cove_book_forge.providers import (
    JsonGeneration,
    ProviderCapabilities,
    ProviderUsage,
    TextGeneration,
)


class _Provider:
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(json_mode=True)

    @property
    def usage(self) -> ProviderUsage:
        return ProviderUsage()

    async def generate_text(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
    ) -> TextGeneration:
        raise AssertionError

    async def generate_json(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_output_tokens: int,
        temperature: float | None = None,
        json_schema: Mapping[str, JsonValue] | None = None,
    ) -> JsonGeneration:
        raise AssertionError

    async def healthcheck(self) -> None:
        return None


def _context(tmp_path: Path) -> AppContext:
    skills = tmp_path / "skills"
    skills.mkdir()
    config = AppConfig.model_validate(
        {
            "library": {"enabled": True, "data_dir": tmp_path / "library"},
            "model": {"provider": "test-provider", "model": "test-model"},
            "outputs": {"skills": {"enabled": True, "canonical_path": skills}},
        }
    )
    library = create_book_library(config)
    return AppContext.create(config, library=library, provider=_Provider())


@pytest.mark.anyio
async def test_protocol_initializes_with_strict_tools_and_local_resources(tmp_path: Path) -> None:
    context = _context(tmp_path)
    imported = context.library.import_book(basic_epub(tmp_path / "book.epub"))
    server = create_mcp_server(context)

    async with create_connected_server_and_client_session(server) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools.tools}
        assert {
            "book_forge_doctor",
            "import_book",
            "list_books",
            "get_book",
            "get_chapter",
            "analyze_chapter",
            "forge_chapter_to_obsidian",
            "forge_chapter_to_skill",
            "plan_book_to_skill",
            "forge_book_to_skill",
            "get_forge_job",
            "control_forge_job",
            "get_book_skill_status",
            "list_generated_skills",
        } <= names
        assert all(tool.outputSchema is not None for tool in tools.tools)

        listed = await client.call_tool("list_books", {})
        assert listed.isError is False
        assert listed.structuredContent is not None
        assert listed.structuredContent["books"][0]["book"]["book_id"] == imported.book.book_id

        resource = await client.read_resource(
            f"cove-book-forge://books/{imported.book.book_id}/chapters/0"
        )
        assert "Readable." in resource.contents[0].text


@pytest.mark.anyio
async def test_tool_errors_are_safe_and_http_is_loopback_only(tmp_path: Path) -> None:
    context = _context(tmp_path)
    async with create_connected_server_and_client_session(create_mcp_server(context)) as client:
        failed = await client.call_tool("get_book", {"book_id": "missing"})
        assert failed.isError is True
        rendered = "".join(getattr(item, "text", "") for item in failed.content)
        assert ForgeErrorCode.SOURCE_NOT_FOUND.value in rendered
        assert str(tmp_path) not in rendered

    with pytest.raises(ForgeException) as caught:
        create_streamable_http_app(context, host="0.0.0.0")
    assert caught.value.code is ForgeErrorCode.CONFIG_INVALID
