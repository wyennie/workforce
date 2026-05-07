"""Per-worker live panels for parallel dispatch.

Renders one rich `Panel` per sub-mission stacked vertically, each showing
the worker's most recent activity and a one-word status. Updates as
messages stream from the asyncio orchestrator.

Use as a context manager. Inside the `with`, hand each worker a callback
from `make_callback(task_id)` for `on_message=`.

Falls back to plain prefixed output if stdout isn't a TTY (CI, pipes, etc.)
— the same `make_callback` returns a stdout-printing callback in that case.
"""

from __future__ import annotations

import sys
from collections import deque
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from workforce.cli._common import _summarize_tool_args, _tool_color, _truncate

_DEFAULT_LINES_PER_PANEL = 15


def _format_message(msg: Any) -> tuple[list[str], str | None]:
    """Return (lines to append to the panel, new status or None to leave)."""
    if isinstance(msg, AssistantMessage):
        lines: list[str] = []
        new_status: str | None = None
        for block in msg.content:
            if isinstance(block, TextBlock):
                text = block.text.strip()
                if text:
                    lines.extend(text.splitlines())
                    new_status = "writing"
            elif isinstance(block, ToolUseBlock):
                args = _truncate(_summarize_tool_args(block.name, block.input), 60)
                color = _tool_color(block.name)
                lines.append(f"[{color}]→ {block.name}[/{color}][dim]  {args}[/dim]")
                new_status = f"tool: {block.name}"
            elif isinstance(block, ThinkingBlock):
                lines.append("[dim]  ⟨ thinking ⟩[/dim]")
                new_status = "thinking"
        return lines, new_status
    if isinstance(msg, UserMessage):
        if isinstance(msg.content, list):
            lines = []
            for block in msg.content:
                if isinstance(block, ToolResultBlock) and block.is_error:
                    preview = _truncate(repr(block.content), 100)
                    lines.append(f"✗ tool error: {preview}")
            return lines, None
        return [], None
    if isinstance(msg, ResultMessage):
        cost = msg.total_cost_usd or 0.0
        lines = [
            f"✓ done — turns={msg.num_turns}, "
            f"duration={msg.duration_ms}ms, cost=${cost:.4f}"
        ]
        status = "done" if not msg.is_error else "error"
        return lines, status
    if isinstance(msg, SystemMessage):
        return [], None
    return [], None


class PanelDisplay:
    """Live per-worker panels. Use as a context manager.

    Example:
        with PanelDisplay(["impl", "tests", "docs"]) as panels:
            await dispatch_parallel(
                ...,
                make_sub_callback=panels.make_callback,
            )
    """

    def __init__(
        self,
        task_ids: list[str],
        *,
        max_lines_per_panel: int = _DEFAULT_LINES_PER_PANEL,
        console: Console | None = None,
    ) -> None:
        """Initialize a PanelDisplay for the given workers.

        Args:
            task_ids: Identifiers for each parallel worker; one panel is created
                per id.
            max_lines_per_panel: Rolling window size for each panel's output
                buffer. Older lines are discarded as new ones arrive.
            console: Rich Console to render into. Defaults to a new Console().
        """
        self.task_ids = list(task_ids)
        self.buffers: dict[str, deque[str]] = {
            tid: deque(maxlen=max_lines_per_panel) for tid in self.task_ids
        }
        self.statuses: dict[str, str] = {tid: "starting" for tid in self.task_ids}
        self._console = console or Console()
        self._live: Live | None = None
        self._tty = self._console.is_terminal
        self._started = False

    def __enter__(self) -> PanelDisplay:
        """Enter the context; the Live display starts lazily on the first message."""
        # Lazy: don't start the Live until the first message arrives.
        # That keeps the terminal free for the confirm prompt that runs
        # before any workers produce output.
        return self

    def __exit__(self, *exc: Any) -> None:
        """Exit the context, flushing a final render and stopping the Live display."""
        self._stop()

    def _start_if_needed(self) -> None:
        """Start the Rich Live display on first call; no-op when not a TTY or already running."""
        if not self._tty or self._started:
            return
        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=4,
            transient=False,
        )
        self._live.start()
        self._started = True

    def _stop(self) -> None:
        """Flush a final render and stop the Rich Live display."""
        if self._started and self._live is not None:
            # Final refresh so done/error lines are visible.
            self._live.update(self._render())
            self._live.stop()
            self._live = None
            self._started = False

    def make_callback(self, task_id: str) -> Callable[[Any], None]:
        """Return an `on_message` callback for one worker.

        In TTY mode: routes into the worker's panel.
        In non-TTY mode: prints prefixed lines to stdout (same shape as the
        old interleaved renderer so CI / piped output stays useful).
        """
        if not self._tty:
            return self._make_plain_callback(task_id)
        return self._make_panel_callback(task_id)

    def _make_panel_callback(self, task_id: str) -> Callable[[Any], None]:
        """Return a callback that routes messages into the TTY panel for *task_id*."""
        def render(msg: Any) -> None:
            self._start_if_needed()
            lines, new_status = _format_message(msg)
            if lines:
                for line in lines:
                    self.buffers[task_id].append(line)
            if new_status is not None:
                self.statuses[task_id] = new_status
            if self._live is not None:
                self._live.update(self._render())
        return render

    def _make_plain_callback(self, task_id: str) -> Callable[[Any], None]:
        """Return a callback that prints prefixed lines to the console (non-TTY mode)."""
        # Escape the brackets so rich doesn't treat `[task]` as a markup tag.
        prefix = f"\\[{task_id}] "

        def render(msg: Any) -> None:
            lines, _ = _format_message(msg)
            for line in lines:
                self._console.print(f"{prefix}{line}")
        return render

    def _render(self) -> Group:
        """Build the Rich Group of panels from current buffers and statuses."""
        panels = []
        for tid in self.task_ids:
            body = "\n".join(self.buffers[tid]) if self.buffers[tid] else "[dim](waiting)[/dim]"
            status = self.statuses[tid]
            status_prefix = status.split(":", 1)[0]
            status_color = {
                "done": "green",
                "error": "red",
                "starting": "dim",
                "thinking": "blue",
                "writing": "cyan",
            }.get(status_prefix, "yellow")
            border_style = {
                "done": "green",
                "error": "red",
                "starting": "dim",
                "thinking": "blue",
                "writing": "cyan",
            }.get(status_prefix, "yellow")
            title = f"[bold]{tid}[/bold] [{status_color}]({status})[/{status_color}]"
            panels.append(Panel(
                Text.from_markup(body),
                title=title,
                title_align="left",
                border_style=border_style,
            ))
        return Group(*panels)


def stdout_is_tty() -> bool:
    """Return ``True`` if stdout is connected to a terminal (not a pipe or redirect)."""
    return sys.stdout.isatty()
