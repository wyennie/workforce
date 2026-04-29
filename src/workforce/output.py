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
_CI_MODE: bool = False


def set_ci_mode() -> None:
    """Switch all output to plain text (no ANSI codes). Called once by --ci."""
    global _stdout, _stderr, _CI_MODE
    _CI_MODE = True
    # color_system=None tells Rich to produce no ANSI escape sequences at all;
    # markup is still parsed and stripped, so callers never see raw tags.
    _stdout = Console(color_system=None)
    _stderr = Console(stderr=True, color_system=None)


def is_ci_mode() -> bool:
    """True when set_ci_mode() has been called for this process."""
    return _CI_MODE


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
