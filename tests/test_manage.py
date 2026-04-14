"""Tests for the Manager chat session helpers.

The interactive loop itself is awkward to test (it's a streaming SDK session
plus stdin), so we focus on the testable units: prompt building, message
rendering, tool summarization. The CLI integration is exercised by a smoke
test that invokes `workforce manage --help`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from workforce.cli import manage
from workforce.project import Project
from workforce.specialist import RosterStore, Specialist


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WORKFORCE_HOME", str(home))
    home.mkdir(parents=True, exist_ok=True)
    return home


@pytest.fixture
def store_with_specialist(isolated_home: Path) -> tuple[RosterStore, Specialist]:
    rs = RosterStore()
    spec = Specialist.from_template("aria", "backend")
    rs.save(spec)
    return rs, spec


def test_prompt_includes_project_name_and_kind(
    isolated_home: Path, store_with_specialist: tuple[RosterStore, Specialist],
) -> None:
    rs, spec = store_with_specialist
    proj = Project(
        id="abc123def456",
        name="myws",
        repo_path="/tmp/myws",
        kind="workspace",
        assigned_specialists=[spec.name],
    )
    prompt = manage._build_manager_prompt(proj, rs)
    assert "myws" in prompt
    assert "workspace" in prompt
    # Specialist roster gets injected
    assert spec.name in prompt
    assert spec.model in prompt


def test_prompt_handles_no_assigned_specialists(
    isolated_home: Path,
) -> None:
    rs = RosterStore()
    proj = Project(
        id="abc123def456",
        name="empty",
        repo_path="/tmp/empty",
        kind="workspace",
        assigned_specialists=[],
    )
    prompt = manage._build_manager_prompt(proj, rs)
    # Should mention there are no specialists assigned, not crash.
    assert "none" in prompt.lower()


def test_prompt_teaches_background_dispatch_and_forbids_window(
    isolated_home: Path, store_with_specialist: tuple[RosterStore, Specialist],
) -> None:
    """The Manager must use --background (one shared tail window owned by the
    chat session), not --window (would open a window per dispatch)."""
    rs, spec = store_with_specialist
    proj = Project(
        id="abc123def456", name="p", repo_path="/tmp/p",
        assigned_specialists=[spec.name],
    )
    prompt = manage._build_manager_prompt(proj, rs)
    assert "--background" in prompt
    assert "workforce dispatch" in prompt
    # Must explicitly tell the Manager NOT to use --window — that breaks the
    # one-window UX.
    assert "DO NOT use `--window`" in prompt or "DO NOT use --window" in prompt
    # Must also teach mission-status checking so the Manager can answer
    # "what's running?"
    assert "mission show" in prompt


def test_prompt_kind_explanation_differs(
    isolated_home: Path, store_with_specialist: tuple[RosterStore, Specialist],
) -> None:
    rs, spec = store_with_specialist
    repo_proj = Project(
        id="abc123def456", name="r", repo_path="/tmp/r",
        kind="repo", assigned_specialists=[spec.name],
    )
    ws_proj = Project(
        id="def456abc789", name="w", repo_path="/tmp/w",
        kind="workspace", assigned_specialists=[spec.name],
    )
    repo_prompt = manage._build_manager_prompt(repo_proj, rs)
    ws_prompt = manage._build_manager_prompt(ws_proj, rs)
    assert "worktree" in repo_prompt or "branch" in repo_prompt
    assert "no commits" in ws_prompt or "edit files directly" in ws_prompt


def test_summarize_tool_bash_truncates() -> None:
    long_cmd = "echo " + "x" * 200
    out = manage._summarize_tool("Bash", {"command": long_cmd})
    assert len(out) <= 80
    assert out.endswith("...")


def test_summarize_tool_file_path_picked() -> None:
    assert manage._summarize_tool("Edit", {"file_path": "/tmp/x.py"}) == "/tmp/x.py"
    assert manage._summarize_tool("Write", {"file_path": "/tmp/y", "content": "..."}) == "/tmp/y"


def test_summarize_tool_falls_back_to_first_arg() -> None:
    out = manage._summarize_tool("Custom", {"thing": "value"})
    assert "thing=" in out


# ----- run_manager_chat: SDK-error-before-ResultMessage deadlock test --------
# The fix lives in the render_loop exception handler: it calls turn_done.set()
# so input_loop is unblocked and gather() can return. Without that fix the
# function would hang because input_loop waits on turn_done indefinitely.


def test_run_manager_chat_sdk_error_does_not_hang(
    isolated_home: Path,
    store_with_specialist: tuple[RosterStore, Specialist],
) -> None:
    """If the SDK raises before yielding a ResultMessage, the chat session
    should exit cleanly (return 0) rather than hanging forever."""
    rs, spec = store_with_specialist
    proj = Project(
        id="abc123def456",
        name="test-proj",
        repo_path="/tmp/test-proj",
        kind="workspace",
        assigned_specialists=[spec.name],
    )

    # Simulate the SDK raising an exception immediately — no ResultMessage ever
    # arrives, so render_loop's turn_done.set() in the except block is the only
    # thing that can unblock input_loop.
    call_count = [0]

    async def exploding_query(prompt: Any, options: Any) -> Any:
        """Async generator that drains one user message from feed() then raises."""
        async for _ in prompt:
            break  # got one message; now simulate transport error
        raise RuntimeError("SDK transport error")
        yield  # unreachable — makes this an async generator

    def fake_input() -> str | None:
        call_count[0] += 1
        if call_count[0] == 1:
            return "hello, what can you do?"
        return None  # EOF on second call — signals clean exit

    # asyncio.to_thread wraps the blocking call; replace it so fake_input runs
    # inline in the event loop without actually spawning a thread.
    async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    with patch.object(manage, "query", exploding_query):
        with patch.object(manage, "_read_user_input", fake_input):
            with patch("workforce.terminal.open_terminal_window", return_value=False):
                with patch("asyncio.to_thread", fake_to_thread):
                    # 5 s should be vastly more than enough; a hang means no fix
                    rc = asyncio.run(
                        asyncio.wait_for(
                            manage.run_manager_chat(proj, rs),
                            timeout=5.0,
                        )
                    )

    assert rc == 0
