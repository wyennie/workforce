"""CLI command for starting the Workforce web dashboard."""

from __future__ import annotations

import typer


def serve_command(
    port: int = typer.Option(8080, "--port", help="TCP port to listen on."),
    host: str = typer.Option("127.0.0.1", "--host", help="IP address to bind."),
    reload: bool = typer.Option(False, "--reload", help="Enable auto-reload (dev mode)."),
) -> None:
    """Start the local Workforce web dashboard.

    Requires the [web] optional extras:

        pip install 'workforce[web]'

    Opens a browser-accessible dashboard at http://<host>:<port>/ showing
    missions, stats, and the specialist roster.
    """
    try:
        from workforce.web.serve import start_server
    except ImportError:
        from workforce import output  # noqa: PLC0415

        output.die(
            "workforce[web] extras not installed. "
            "Run: pip install 'workforce[web]'"
        )
        return  # unreachable; silences mypy

    start_server(host=host, port=port, reload=reload)
