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
