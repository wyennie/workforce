"""CLI commands for the specialist marketplace: install, publish, search."""

from __future__ import annotations

import json
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import typer
from rich.table import Table

from workforce import output, paths
from workforce.specialist import NAME_PATTERN, RosterError, RosterStore, Specialist
from workforce.utils import _dump_toml

#: Default registry base URL (raw GitHub content for workforce-ai/specialists).
DEFAULT_REGISTRY = "https://raw.githubusercontent.com/workforce-ai/specialists/main"

sub = typer.Typer(
    name="specialist",
    help="Browse and install specialists from the marketplace.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _FetchError(Exception):
    """Raised when a network fetch fails."""


def _fetch(url: str) -> bytes:
    """Fetch *url* and return raw bytes.

    Args:
        url: The URL to retrieve.

    Returns:
        The response body as bytes.

    Raises:
        _FetchError: If the request fails for any network reason.
    """
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
            return resp.read()
    except urllib.error.URLError as exc:
        raise _FetchError(f"could not reach {url}: {exc}") from exc


def _store() -> RosterStore:
    """Ensure the Workforce layout exists and return a RosterStore."""
    paths.ensure_layout()
    return RosterStore()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@sub.command("install")
def install(
    slug: str = typer.Argument(..., help="Specialist slug in the registry (e.g. 'backend-go')."),
    name: str | None = typer.Option(
        None,
        "--name",
        help="Local name to install the specialist as (default: the registry slug).",
    ),
    registry_url: str = typer.Option(
        DEFAULT_REGISTRY,
        "--registry-url",
        help="Base URL of the specialist registry.",
        show_default=True,
    ),
) -> None:
    """Download and install a specialist from the marketplace.

    Fetches the specialist definition from the registry, lets you choose a
    local name, and saves it into your roster.  Prompts before overwriting an
    existing specialist.

    After installing, assign the specialist to a project with:

        workforce project assign <project> <name>
    """
    url = f"{registry_url.rstrip('/')}/specialists/{slug}/specialist.toml"
    output.info(f"fetching {url} …")

    try:
        content = _fetch(url)
    except _FetchError as exc:
        output.die(str(exc))

    try:
        data = tomllib.loads(content.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        output.die(f"invalid TOML from registry: {exc}")

    # Determine install name: if not provided via --name, prompt with the
    # slug as the default.
    chosen = name
    if chosen is None:
        chosen = typer.prompt("Install as name", default=slug)

    if not NAME_PATTERN.match(chosen):
        output.die(
            f"invalid name {chosen!r}: must start with a lowercase letter and "
            "contain only lowercase letters, digits, '-' or '_' (max 32 chars)"
        )

    # Replace the registry name with the chosen local name before validation.
    data["name"] = chosen

    try:
        spec = Specialist.model_validate(data)
    except ValueError as exc:
        output.die(f"invalid specialist definition from registry: {exc}")

    store = _store()
    should_overwrite = False
    if store.exists(chosen):
        should_overwrite = typer.confirm(
            f"Specialist {chosen!r} already exists. Overwrite?", default=False
        )
        if not should_overwrite:
            output.info("aborted")
            raise typer.Exit()

    try:
        store.save(spec, overwrite=should_overwrite)
    except RosterError as exc:
        output.die(str(exc))

    output.success(f"installed specialist {chosen!r}")
    output.info(
        f"[dim]Assign to a project with: "
        f"workforce project assign <project> {chosen}[/dim]"
    )


@sub.command("publish")
def publish(
    name: str = typer.Argument(..., help="Name of the specialist in your local roster."),
    output_dir: Path | None = typer.Option(
        None,
        "--output-dir",
        help="Directory to write files into (default: ./specialists/<name>/).",
    ),
) -> None:
    """Export a local specialist for publishing to the marketplace.

    Writes ``specialist.toml`` and a ``README.md`` stub to the output
    directory.  The resulting directory can be dropped directly into a fork
    of ``workforce-ai/specialists`` and submitted as a pull request.

    Memory and stats are never included — they are runtime-only artifacts
    stored in separate files locally and have no place in the registry.
    """
    store = _store()
    try:
        spec = store.load(name)
    except RosterError as exc:
        output.die(str(exc))

    dest = output_dir or Path("specialists") / name
    dest.mkdir(parents=True, exist_ok=True)

    # specialist.toml — identical structure to the local copy; memory/stats
    # live in separate files and are not included in the model_dump().
    toml_text = _dump_toml(spec.model_dump(exclude_none=True))
    (dest / "specialist.toml").write_text(toml_text)

    # Minimal README stub.
    readme = (
        f"# {name}\n\n"
        f"{spec.role}\n\n"
        "## Usage\n\n"
        "Install via the Workforce specialist marketplace:\n\n"
        "```\n"
        f"workforce specialist install {name}\n"
        "```\n\n"
        "## Details\n\n"
        f"Model: `{spec.model}`  \n"
        f"Tools: {', '.join(spec.allowed_tools)}\n"
    )
    (dest / "README.md").write_text(readme)

    output.success(f"exported {name!r} → {dest}/")
    output.info(
        "To publish to the marketplace:\n"
        "  1. Fork https://github.com/workforce-ai/specialists\n"
        f"  2. Copy {dest}/ to specialists/{name}/ in your fork\n"
        "  3. Add an entry to specialists/index.json\n"
        "  4. Open a pull request"
    )


@sub.command("search")
def search(
    query: str | None = typer.Argument(
        None,
        help="Filter by slug or description (case-insensitive). Omit to list all.",
    ),
    registry_url: str = typer.Option(
        DEFAULT_REGISTRY,
        "--registry-url",
        help="Base URL of the specialist registry.",
        show_default=True,
    ),
) -> None:
    """Search the specialist marketplace.

    Fetches the registry index and displays matching entries in a table.
    If the registry is unreachable (no internet, registry not yet live) a
    friendly message is printed instead of an error.

    Examples:

        workforce specialist search
        workforce specialist search go
        workforce specialist search "frontend react"
    """
    url = f"{registry_url.rstrip('/')}/specialists/index.json"

    try:
        content = _fetch(url)
    except _FetchError as exc:
        output.warn(
            f"could not reach the marketplace ({exc}). "
            "Check your internet connection or try again later."
        )
        raise typer.Exit()

    try:
        entries: list[dict] = json.loads(content.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        output.die(f"invalid index from registry: {exc}")

    if not isinstance(entries, list):
        output.die("unexpected registry index format (expected a JSON array)")

    if query:
        q = query.lower()
        entries = [
            e for e in entries
            if q in e.get("slug", "").lower()
            or q in e.get("description", "").lower()
        ]

    if not entries:
        if query:
            output.info(f"no specialists found matching {query!r}")
        else:
            output.info("the marketplace has no entries yet")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("slug")
    table.add_column("description", overflow="fold")
    table.add_column("templates_used")

    for entry in entries:
        templates = ", ".join(entry.get("templates_used") or [])
        table.add_row(
            entry.get("slug", ""),
            entry.get("description", ""),
            templates,
        )

    output.print_table(table)
    output.info("[dim]Install one with: workforce specialist install <slug>[/dim]")
