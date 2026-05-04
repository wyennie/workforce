"""Uvicorn launcher for the Workforce web dashboard."""

from __future__ import annotations


def start_server(host: str, port: int, reload: bool = False) -> None:
    """Start the Workforce web dashboard using uvicorn.

    Args:
        host: IP address to bind (e.g. ``"127.0.0.1"``).
        port: TCP port to listen on.
        reload: Enable uvicorn's auto-reload for development.

    Raises:
        ImportError: If uvicorn is not installed (``workforce[web]`` extras
            required).
    """
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "workforce[web] extras required: pip install 'workforce[web]'"
        ) from exc

    uvicorn.run(
        "workforce.web.app:app",
        host=host,
        port=port,
        reload=reload,
    )
