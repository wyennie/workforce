"""CLI command group for `workforce ticket`.

Provides `workforce ticket new` to scaffold a ticket from a template, open it
in the user's editor, and optionally dispatch it straight away.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import typer

from workforce import output
from workforce.ticket_templates import TICKET_TEMPLATES

sub = typer.Typer(
    name="ticket",
    help="Create and manage tickets.",
    no_args_is_help=True,
)

_AVAILABLE = sorted(TICKET_TEMPLATES.keys())


def _find_editor() -> str:
    """Return the editor to use, falling back through EDITOR → nano → vi."""
    editor = os.environ.get("EDITOR", "").strip()
    if editor:
        return editor
    for fallback in ("nano", "vi"):
        if _which(fallback):
            return fallback
    return "vi"  # last resort; vi ships on virtually every Unix


def _which(name: str) -> str | None:
    """Return the full path of *name* if it is on PATH, else None."""
    import shutil
    return shutil.which(name)


@sub.command("new")
def new_command(
    ticket_type: str | None = typer.Argument(
        None,
        metavar="TYPE",
        help="Ticket type (bug-fix, feature, refactor, chore, docs).",
    ),
    list_types: bool = typer.Option(
        False, "--list", "-l", help="List available ticket types and exit."
    ),
) -> None:
    """Create a new ticket from a template and open it in your editor.

    If TYPE is omitted you will be prompted to choose.  After the editor
    closes, the ticket is printed and you are asked whether to dispatch it.
    The temp file is kept so you can use it manually:

        workforce dispatch <project> --file /tmp/workforce-ticket-XXXX.md
    """
    if list_types:
        output.info("Available ticket types:")
        for t in _AVAILABLE:
            output.info(f"  {t}")
        return

    if ticket_type is None:
        output.info("Available types: " + ", ".join(_AVAILABLE))
        ticket_type = typer.prompt("Ticket type")

    if ticket_type not in TICKET_TEMPLATES:
        output.die(
            f"Unknown ticket type {ticket_type!r}. "
            "Available: " + ", ".join(_AVAILABLE)
        )

    template = TICKET_TEMPLATES[ticket_type]

    # Write template to a named temp file (not deleted on close so the editor
    # can open it and we can read it back after).
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        prefix="workforce-ticket-", suffix=".md", dir=os.environ.get("TMPDIR")
    )
    tmp_path = Path(tmp_path_str)
    try:
        os.write(tmp_fd, template.encode())
    finally:
        os.close(tmp_fd)

    editor = _find_editor()
    try:
        subprocess.run([editor, str(tmp_path)], check=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        output.die(f"Editor failed: {exc}")

    content = tmp_path.read_text()

    if not content.strip() or content == template:
        output.info("Ticket cancelled (content unchanged or empty).")
        return

    output.rule("ticket content")
    output.raw(content)
    output.rule()

    dispatch = typer.confirm("Dispatch now?", default=False)
    if dispatch:
        output.info(
            f"Run:  workforce dispatch <project> --file {tmp_path}"
        )
    else:
        output.info(f"Ticket saved to: {tmp_path}")
