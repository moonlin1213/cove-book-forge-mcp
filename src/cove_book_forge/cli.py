import json
from pathlib import Path
from typing import Annotated

import typer

from cove_book_forge.doctor import run_doctor

app = typer.Typer(no_args_is_help=True, help="Forge books into reusable knowledge.")


@app.callback()
def main() -> None:
    """Forge books into reusable knowledge."""


@app.command()
def doctor(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report read-only configuration and local-directory diagnostics."""
    report = run_doctor(config)
    if json_output:
        payload = {
            "ok": report.ok,
            "checks": [item.model_dump(mode="json") for item in report.checks],
        }
        typer.echo(json.dumps(payload, ensure_ascii=False))
    else:
        for check in report.checks:
            typer.echo(f"{check.status.value.upper():4} {check.name}: {check.message}")
    raise typer.Exit(code=0 if report.ok else 1)


@app.command("mcp")
def mcp_command(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    transport: Annotated[str, typer.Option("--transport")] = "stdio",
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
) -> None:
    """Run the MCP server over stdio or explicit loopback Streamable HTTP."""
    from cove_book_forge.config import load_config
    from cove_book_forge.errors import ForgeException
    from cove_book_forge.mcp_server import AppContext, create_mcp_server, create_streamable_http_app

    try:
        context = AppContext.create(load_config(config))
        if transport == "stdio":
            create_mcp_server(context).run(transport="stdio")
            return
        if transport == "http":
            create_streamable_http_app(context, host=host, port=port)
            create_mcp_server(context, host=host, port=port).run(transport="streamable-http")
            return
        raise typer.BadParameter("transport must be 'stdio' or 'http'")
    except ForgeException as exc:
        typer.echo(exc.as_detail().model_dump_json(), err=True)
        raise typer.Exit(code=2) from None
