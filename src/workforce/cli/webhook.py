"""CLI commands for the webhook daemon: start, status, stop."""

from __future__ import annotations

import os
import signal
from pathlib import Path

import typer

from workforce import output, paths

sub = typer.Typer(
    name="webhook",
    help="Manage the GitHub webhook listener daemon.",
    no_args_is_help=True,
)


def _pid_file() -> Path:
    """Return the path to the webhook PID file."""
    return paths.home() / "webhook.pid"


def _read_pid() -> int | None:
    """Read the PID from the webhook.pid file.

    Returns:
        The PID as an int, or None if the file doesn't exist or is invalid.
    """
    pid_path = _pid_file()
    if not pid_path.is_file():
        return None
    text = pid_path.read_text().strip()
    try:
        return int(text)
    except ValueError:
        return None


def _is_running(pid: int) -> bool:
    """Check whether a process with the given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user.
        return True


@sub.command("start")
def start(
    port: int = typer.Option(8080, "--port", "-p", help="TCP port to listen on."),
    host: str = typer.Option("0.0.0.0", "--host", help="Host/address to bind."),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Path to webhook.toml (default: ~/.workforce/webhook.toml).",
    ),
) -> None:
    """Start the webhook daemon with uvicorn.

    Writes the server PID to ~/.workforce/webhook.pid so ``webhook status``
    and ``webhook stop`` can manage it.
    """
    try:
        import uvicorn
    except ImportError:
        output.die(
            "uvicorn is not installed. "
            "Install it with: pip install 'workforce-ai[webhook]'"
        )

    from workforce.webhook.config import load_webhook_config

    # Validate the config before starting so bad configs surface immediately.
    cfg_path: Path | None = config
    if cfg_path is None:
        env_path = os.environ.get("WORKFORCE_WEBHOOK_CONFIG")
        if env_path:
            cfg_path = Path(env_path)
        else:
            cfg_path = paths.home() / "webhook.toml"

    try:
        load_webhook_config(cfg_path)
    except FileNotFoundError:
        output.die(
            f"webhook config not found at {cfg_path}. "
            "Create it first — see `workforce webhook --help` or docs/webhook.md."
        )
    except Exception as e:
        output.die(f"invalid webhook config: {e}")

    pid_path = _pid_file()
    existing_pid = _read_pid()
    if existing_pid is not None and _is_running(existing_pid):
        output.die(
            f"webhook daemon is already running (PID {existing_pid}). "
            "Run `workforce webhook stop` first."
        )

    # Write our own PID so status/stop can find us.
    pid_path.write_text(str(os.getpid()) + "\n")

    if cfg_path is not None:
        os.environ["WORKFORCE_WEBHOOK_CONFIG"] = str(cfg_path)

    output.info(f"[bold]webhook daemon[/bold] listening on {host}:{port}")
    output.info(f"  config: {cfg_path}")
    output.info(f"  pid:    {os.getpid()}")

    try:
        uvicorn.run(
            "workforce.webhook.server:app",
            host=host,
            port=port,
            log_level="info",
        )
    finally:
        # Clean up PID file on exit.
        pid_path.unlink(missing_ok=True)


@sub.command("status")
def status() -> None:
    """Check whether the webhook daemon is running."""
    pid = _read_pid()
    if pid is None:
        output.info("[yellow]stopped[/yellow]  (no PID file found)")
        return

    if _is_running(pid):
        output.info(f"[green]running[/green]   PID {pid}")
    else:
        output.warn(
            f"stale PID file (PID {pid} is not running). "
            "Remove it with: rm ~/.workforce/webhook.pid"
        )


@sub.command("stop")
def stop() -> None:
    """Send SIGTERM to the running webhook daemon."""
    pid = _read_pid()
    if pid is None:
        output.info("webhook daemon is not running (no PID file).")
        return

    if not _is_running(pid):
        output.warn(f"PID {pid} is not running; removing stale PID file.")
        _pid_file().unlink(missing_ok=True)
        return

    try:
        os.kill(pid, signal.SIGTERM)
        output.success(f"sent SIGTERM to webhook daemon (PID {pid})")
    except PermissionError:
        output.die(f"permission denied sending SIGTERM to PID {pid}")
    except ProcessLookupError:
        output.info(f"PID {pid} already exited.")
        _pid_file().unlink(missing_ok=True)
