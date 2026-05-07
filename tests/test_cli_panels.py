"""Tests for the panel display: message formatting + non-TTY fallback."""

from __future__ import annotations

import io
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
from rich.console import Console

from workforce.cli.panels import PanelDisplay, _format_message


def _assistant_text(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="m", parent_tool_use_id=None, error=None, usage=None,
        message_id="m", stop_reason=None, session_id="s", uuid=None,
    )


def _assistant_tool_use(name: str, args: dict[str, Any]) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id="t", name=name, input=args)],
        model="m", parent_tool_use_id=None, error=None, usage=None,
        message_id="m", stop_reason=None, session_id="s", uuid=None,
    )


def _user_tool_error(content: str) -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id="t", content=content, is_error=True)],
        uuid=None, parent_tool_use_id=None, tool_use_result=None,
    )


def _result(*, is_error: bool = False, cost: float = 0.10, turns: int = 3) -> ResultMessage:
    return ResultMessage(
        subtype="success" if not is_error else "error",
        duration_ms=1500, duration_api_ms=1200, is_error=is_error,
        num_turns=turns, session_id="s", stop_reason=None,
        total_cost_usd=cost, usage=None, result=None,
        structured_output=None, model_usage=None, permission_denials=None,
        errors=None, uuid=None,
    )


# ----- _format_message ------------------------------------------------------


def test_format_text_returns_lines_and_writing_status() -> None:
    lines, status = _format_message(_assistant_text("hello\nworld"))
    assert lines == ["hello", "world"]
    assert status == "writing"


def test_format_tool_use_returns_arrow_line_and_tool_status() -> None:
    lines, status = _format_message(_assistant_tool_use("Bash", {"command": "ls"}))
    assert len(lines) == 1
    assert "→ Bash" in lines[0]
    assert status == "tool: Bash"


def test_format_tool_error_marked_with_x() -> None:
    lines, status = _format_message(_user_tool_error("EACCES: denied"))
    assert any("✗ tool error" in line for line in lines)
    assert status is None


def test_format_result_marks_done_status() -> None:
    lines, status = _format_message(_result(cost=0.42, turns=7))
    assert any("done" in line for line in lines)
    assert "0.4200" in lines[0]
    assert status == "done"


def test_format_result_error_marks_error_status() -> None:
    _, status = _format_message(_result(is_error=True))
    assert status == "error"


def test_format_thinking_block_shows_indicator() -> None:
    msg = AssistantMessage(
        content=[ThinkingBlock(thinking="internal", signature="")],
        model="m", parent_tool_use_id=None, error=None, usage=None,
        message_id="m", stop_reason=None, session_id="s", uuid=None,
    )
    lines, status = _format_message(msg)
    assert len(lines) == 1
    assert "thinking" in lines[0]
    assert status == "thinking"


def test_format_system_message_skipped() -> None:
    lines, status = _format_message(SystemMessage(subtype="init", data={}))
    assert lines == []
    assert status is None


# ----- non-TTY fallback -----------------------------------------------------


def test_non_tty_callback_prints_prefixed_lines() -> None:
    """When stdout isn't a TTY, callbacks print prefixed plain text."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=200)
    pd = PanelDisplay(["impl", "tests"], console=console)
    cb = pd.make_callback("impl")
    cb(_assistant_text("hello"))
    cb(_assistant_tool_use("Bash", {"command": "ls"}))

    out = buf.getvalue()
    assert "[impl]" in out
    assert "hello" in out
    assert "→ Bash" in out


def test_panel_callback_buffers_and_updates_status_on_tty() -> None:
    """In TTY mode the callback fills the per-task buffer + updates status."""
    buf = io.StringIO()
    # Force a fake TTY so the panel branch is exercised.
    console = Console(file=buf, force_terminal=True, width=120)
    pd = PanelDisplay(["impl"], console=console)
    cb = pd.make_callback("impl")
    with pd:
        cb(_assistant_text("starting work"))
        cb(_assistant_tool_use("Read", {"file_path": "x.py"}))
        cb(_result(cost=0.05, turns=2))

    assert "starting work" in pd.buffers["impl"]
    assert any("Read" in line for line in pd.buffers["impl"])
    assert pd.statuses["impl"] == "done"


def test_panel_buffer_evicts_old_lines() -> None:
    # Force a fake TTY before constructing callbacks so the panel branch
    # is exercised.
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, width=120)
    pd = PanelDisplay(["a"], max_lines_per_panel=3, console=console)
    cb = pd.make_callback("a")
    with pd:
        for i in range(5):
            cb(_assistant_text(f"line-{i}"))
    # Only the last 3 should remain in the deque
    assert list(pd.buffers["a"]) == ["line-2", "line-3", "line-4"]
