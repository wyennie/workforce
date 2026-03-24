"""Single output module.

Every user-visible message goes through here so we can swap rich for plain
output (or capture for tests) without touching call sites.
"""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console
from rich.table import Table

_stdout = Console()
_stderr = Console(stderr=True)


def info(message: str) -> None:
    _stdout.print(message)


def success(message: str) -> None:
    _stdout.print(f"[green]✓[/green] {message}")


def warn(message: str) -> None:
    _stderr.print(f"[yellow]![/yellow] {message}")


def fail(message: str) -> None:
    _stderr.print(f"[red]✗[/red] {message}")


def rule(title: str = "") -> None:
    _stdout.rule(title)


def print_table(table: Table) -> None:
    _stdout.print(table)


def raw(obj: Any) -> None:
    """Escape hatch for rich renderables."""
    _stdout.print(obj)


def die(message: str, code: int = 1) -> None:
    fail(message)
    sys.exit(code)
