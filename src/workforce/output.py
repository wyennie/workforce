"""Single output module.

Every user-visible message goes through here so we can swap rich for plain
output (or capture for tests) without touching call sites.
"""

from __future__ import annotations

import sys
from typing import Any, NoReturn

from rich.console import Console
from rich.table import Table

_stdout = Console()
_stderr = Console(stderr=True)


def info(message: str) -> None:
    """Print an informational message to stdout (rich markup allowed)."""
    _stdout.print(message)


def success(message: str) -> None:
    """Print a green ✓ success message to stdout."""
    _stdout.print(f"[green]✓[/green] {message}")


def warn(message: str) -> None:
    """Print a yellow ! warning to stderr."""
    _stderr.print(f"[yellow]![/yellow] {message}")


def fail(message: str) -> None:
    """Print a red ✗ failure message to stderr."""
    _stderr.print(f"[red]✗[/red] {message}")


def rule(title: str = "") -> None:
    """Print a horizontal rule to stdout, with an optional title."""
    _stdout.rule(title)


def print_table(table: Table) -> None:
    """Render a rich Table to stdout."""
    _stdout.print(table)


def raw(obj: Any) -> None:
    """Escape hatch for rich renderables (Panel, Group, etc.)."""
    _stdout.print(obj)


def die(message: str, code: int = 1) -> NoReturn:
    """Print a failure message and exit with *code* (default 1)."""
    fail(message)
    sys.exit(code)
