"""Tests for the specialist runner.

We mock at the SDK boundary (`claude_agent_sdk.query`) — no real API calls.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from workforce import runner
from workforce.runner import RunLimits, RunStatus, run_specialist
from workforce.specialist import Specialist

# ----- helpers ---------------------------------------------------------------


def make_spec() -> Specialist:
    return Specialist.from_template("aria", "backend")


def _result(*, is_error: bool = False, cost: float = 0.10, turns: int = 3, errors: list[str] | None = None) -> ResultMessage:
    return ResultMessage(
        subtype="success" if not is_error else "error",
        duration_ms=1500,
        duration_api_ms=1200,
        is_error=is_error,
        num_turns=turns,
        session_id="s1",
        stop_reason=None,
        total_cost_usd=cost,
        usage=None,
        result=None,
        structured_output=None,
        model_usage=None,
        permission_denials=None,
        errors=errors,
        uuid=None,
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-6",
        parent_tool_use_id=None,
        error=None,
        usage=None,
        message_id="m1",
        stop_reason=None,
        session_id="s1",
        uuid=None,
    )


def fake_query_factory(
    messages: list[Any], delay: float = 0.0
) -> tuple[Any, dict[str, Any]]:
    """Returns a stand-in for `claude_agent_sdk.query` that yields the given messages."""
    captured: dict[str, Any] = {}

    def fake_query(*, prompt: str, options: ClaudeAgentOptions, **_: Any) -> AsyncIterator[Any]:
        captured["prompt"] = prompt
        captured["options"] = options

        async def gen() -> AsyncIterator[Any]:
            for m in messages:
                if delay:
                    await asyncio.sleep(delay)
                yield m

        return gen()

    return fake_query, captured


# ----- happy path ------------------------------------------------------------


def test_run_returns_completed(tmp_path: Path) -> None:
    msgs = [
        SystemMessage(subtype="init", data={}),
        _assistant("hello"),
        _result(cost=0.25, turns=4),
    ]
    fake, captured = fake_query_factory(msgs)
    with patch.object(runner, "query", fake):
        result = asyncio.run(
            run_specialist(
                spec=make_spec(),
                system_prompt="sys",
                user_prompt="do the thing",
                cwd=tmp_path,
            )
        )
    assert result.status is RunStatus.COMPLETED
    assert result.cost_usd == pytest.approx(0.25)
    assert result.turn_count == 4
    assert result.final is not None
    assert result.duration_seconds >= 0
    assert result.error_detail is None


def test_run_passes_options_through(tmp_path: Path) -> None:
    spec = make_spec()
    fake, captured = fake_query_factory([_result()])
    with patch.object(runner, "query", fake):
        asyncio.run(
            run_specialist(
                spec=spec,
                system_prompt="composed-sys",
                user_prompt="ticket",
                cwd=tmp_path,
                limits=RunLimits(max_turns=7, max_budget_usd=2.50, max_wall_seconds=600),
            )
        )
    opts: ClaudeAgentOptions = captured["options"]
    assert opts.cwd == str(tmp_path)
    assert opts.system_prompt == "composed-sys"
    assert opts.allowed_tools == spec.allowed_tools
    assert opts.model == spec.model
    assert opts.max_turns == 7
    assert opts.max_budget_usd == pytest.approx(2.50)
    assert opts.permission_mode == "bypassPermissions"
    assert captured["prompt"] == "ticket"


# ----- streaming -------------------------------------------------------------


def test_events_log_written_per_message(tmp_path: Path) -> None:
    msgs = [
        SystemMessage(subtype="init", data={"k": 1}),
        _assistant("hi"),
        _result(),
    ]
    log = tmp_path / "events.jsonl"
    fake, _ = fake_query_factory(msgs)
    with patch.object(runner, "query", fake):
        asyncio.run(
            run_specialist(
                spec=make_spec(),
                system_prompt="sys",
                user_prompt="do",
                cwd=tmp_path,
                events_log=log,
            )
        )
    lines = log.read_text().splitlines()
    assert len(lines) == 3
    types = [json.loads(line)["_type"] for line in lines]
    assert types == ["SystemMessage", "AssistantMessage", "ResultMessage"]


def test_on_message_callback_invoked(tmp_path: Path) -> None:
    msgs = [_assistant("a"), _assistant("b"), _result()]
    seen: list[str] = []
    fake, _ = fake_query_factory(msgs)
    with patch.object(runner, "query", fake):
        asyncio.run(
            run_specialist(
                spec=make_spec(),
                system_prompt="sys",
                user_prompt="do",
                cwd=tmp_path,
                on_message=lambda m: seen.append(type(m).__name__),
            )
        )
    assert seen == ["AssistantMessage", "AssistantMessage", "ResultMessage"]


def test_log_and_callback_independent(tmp_path: Path) -> None:
    """Either, both, or neither — runner must work in all combinations."""
    msgs = [_result()]
    fake, _ = fake_query_factory(msgs)
    with patch.object(runner, "query", fake):
        # Neither
        r = asyncio.run(
            run_specialist(spec=make_spec(), system_prompt="s", user_prompt="u", cwd=tmp_path)
        )
        assert r.status is RunStatus.COMPLETED


# ----- error paths -----------------------------------------------------------


def test_no_result_message_is_error(tmp_path: Path) -> None:
    fake, _ = fake_query_factory([_assistant("a")])  # no ResultMessage
    with patch.object(runner, "query", fake):
        result = asyncio.run(
            run_specialist(
                spec=make_spec(), system_prompt="s", user_prompt="u", cwd=tmp_path
            )
        )
    assert result.status is RunStatus.ERROR
    assert result.final is None
    assert result.error_detail is not None and "ResultMessage" in result.error_detail


def test_is_error_result_propagates(tmp_path: Path) -> None:
    fake, _ = fake_query_factory([_result(is_error=True, errors=["boom", "bang"])])
    with patch.object(runner, "query", fake):
        result = asyncio.run(
            run_specialist(
                spec=make_spec(), system_prompt="s", user_prompt="u", cwd=tmp_path
            )
        )
    assert result.status is RunStatus.ERROR
    assert result.error_detail is not None
    assert "boom" in result.error_detail
    assert "bang" in result.error_detail


def test_sdk_exception_becomes_error_result(tmp_path: Path) -> None:
    """If the SDK raises mid-stream, runner returns an ERROR — doesn't crash."""

    def fake_query(*, prompt: str, options: Any, **_: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            # Yield one message, then explode (mimics SDK subprocess failure).
            yield SystemMessage(subtype="init", data={})
            raise Exception("Command failed with exit code 1")

        return gen()

    with patch.object(runner, "query", fake_query):
        result = asyncio.run(
            run_specialist(
                spec=make_spec(),
                system_prompt="s",
                user_prompt="u",
                cwd=tmp_path,
            )
        )
    assert result.status is RunStatus.ERROR
    assert result.error_detail is not None
    assert "sdk error" in result.error_detail
    assert "exit code 1" in result.error_detail


def test_sdk_exception_writes_stderr_log(tmp_path: Path) -> None:
    """Stderr capture file is created next to events.jsonl."""
    log = tmp_path / "events.jsonl"

    def fake_query(*, prompt: str, options: Any, **_: Any) -> AsyncIterator[Any]:
        # Simulate the SDK invoking the stderr callback before failing.
        if options.stderr is not None:
            options.stderr("real diagnostic from claude CLI")

        async def gen() -> AsyncIterator[Any]:
            raise Exception("boom")
            yield  # unreachable, satisfies type

        return gen()

    with patch.object(runner, "query", fake_query):
        result = asyncio.run(
            run_specialist(
                spec=make_spec(),
                system_prompt="s",
                user_prompt="u",
                cwd=tmp_path,
                events_log=log,
            )
        )
    assert result.status is RunStatus.ERROR
    stderr_path = log.with_name("stderr.log")
    assert stderr_path.is_file()
    assert "real diagnostic" in stderr_path.read_text()
    assert "stderr.log" in (result.error_detail or "")


def test_wall_timeout(tmp_path: Path) -> None:
    """A slow stream should be cut off when max_wall_seconds elapses."""
    msgs = [_assistant("a"), _result()]
    # 10 second delay between messages, but wall limit of 0.05s.
    fake, _ = fake_query_factory(msgs, delay=10.0)
    with patch.object(runner, "query", fake):
        result = asyncio.run(
            run_specialist(
                spec=make_spec(),
                system_prompt="s",
                user_prompt="u",
                cwd=tmp_path,
                limits=RunLimits(max_wall_seconds=0.05),
            )
        )
    assert result.status is RunStatus.WALL_TIMEOUT
    assert result.error_detail is not None and "wall time" in result.error_detail


# ----- serialization ---------------------------------------------------------


def test_message_to_jsonable_includes_type_tag() -> None:
    d = runner.message_to_jsonable(_assistant("hi"))
    assert d["_type"] == "AssistantMessage"
    assert d["model"] == "claude-sonnet-4-6"
    assert d["content"][0]["text"] == "hi"


def test_message_to_jsonable_handles_non_dataclass() -> None:
    d = runner.message_to_jsonable("not a dataclass")
    assert d["_type"] == "str"
    assert "repr" in d


# ----- _single_message_stream ------------------------------------------------


def test_single_message_stream_shape() -> None:
    """Regression: verify the dict format emitted by _single_message_stream.

    If the SDK changes the internal streaming-input message format, the
    can_use_tool path breaks silently.  This test documents the expected shape
    so that a format change surfaces as an explicit test failure rather than a
    mysterious runtime error.  See the TODO comment in runner.py above the
    function definition.
    """

    async def collect() -> list[dict[str, Any]]:
        return [m async for m in runner._single_message_stream("hello world")]

    msgs = asyncio.run(collect())
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg["type"] == "user"
    assert msg["message"]["role"] == "user"
    assert msg["message"]["content"] == "hello world"
    assert "session_id" in msg
    assert "parent_tool_use_id" in msg


# ----- can_use_tool path ------------------------------------------------------


def test_can_use_tool_uses_streaming_protocol(tmp_path: Path) -> None:
    """When can_use_tool is set, the prompt must be an AsyncIterable and
    permission_mode must be 'default' (not 'bypassPermissions').
    """
    from claude_agent_sdk import PermissionResultAllow

    captured: dict[str, Any] = {}

    async def my_callback(
        tool_name: str, tool_input: dict[str, Any], ctx: Any
    ) -> PermissionResultAllow:
        return PermissionResultAllow()

    def fake_query_cap(
        *, prompt: Any, options: ClaudeAgentOptions, **_: Any
    ) -> Any:
        captured["prompt"] = prompt
        captured["permission_mode"] = options.permission_mode
        captured["can_use_tool"] = options.can_use_tool

        async def gen() -> Any:
            # consume the async iterable prompt if present
            if hasattr(prompt, "__aiter__"):
                async for _ in prompt:
                    pass
            yield _result()

        return gen()

    with patch.object(runner, "query", fake_query_cap):
        asyncio.run(
            run_specialist(
                spec=make_spec(),
                system_prompt="sys",
                user_prompt="hello",
                cwd=tmp_path,
                can_use_tool=my_callback,
            )
        )

    # Prompt must be an async iterable, not a plain string.
    assert not isinstance(captured["prompt"], str)
    assert hasattr(captured["prompt"], "__aiter__")
    # permission_mode must be "default" so the callback fires.
    assert captured["permission_mode"] == "default"
    # The callback reference must be threaded through to options.
    assert captured["can_use_tool"] is my_callback


def test_can_use_tool_callback_is_invoked(tmp_path: Path) -> None:
    """The can_use_tool callback must be called for each simulated tool use."""
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    calls: list[tuple[str, dict[str, Any]]] = []

    async def my_callback(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow:
        calls.append((tool_name, tool_input))
        return PermissionResultAllow()

    def fake_query_with_callback(
        *, prompt: Any, options: ClaudeAgentOptions, **_: Any
    ) -> Any:
        """Fake SDK that simulates a tool-use permission check, then yields."""

        async def gen() -> Any:
            # consume prompt stream
            if hasattr(prompt, "__aiter__"):
                async for _ in prompt:
                    pass
            # simulate SDK calling can_use_tool before executing a tool
            if options.can_use_tool is not None:
                ctx = ToolPermissionContext(
                    signal=None, suggestions=[], tool_use_id="sim-1"
                )
                await options.can_use_tool("Read", {"file_path": "foo.py"}, ctx)
            yield _result()

        return gen()

    with patch.object(runner, "query", fake_query_with_callback):
        result = asyncio.run(
            run_specialist(
                spec=make_spec(),
                system_prompt="sys",
                user_prompt="do the thing",
                cwd=tmp_path,
                can_use_tool=my_callback,
            )
        )

    assert result.status is RunStatus.COMPLETED
    assert len(calls) == 1
    assert calls[0] == ("Read", {"file_path": "foo.py"})


def test_no_can_use_tool_uses_string_prompt(tmp_path: Path) -> None:
    """Without can_use_tool, the prompt stays a plain string (no overhead)."""
    captured: dict[str, Any] = {}

    def fake_query_cap(
        *, prompt: Any, options: ClaudeAgentOptions, **_: Any
    ) -> Any:
        captured["prompt"] = prompt
        captured["permission_mode"] = options.permission_mode

        async def gen() -> Any:
            yield _result()

        return gen()

    with patch.object(runner, "query", fake_query_cap):
        asyncio.run(
            run_specialist(
                spec=make_spec(),
                system_prompt="sys",
                user_prompt="hello",
                cwd=tmp_path,
                # no can_use_tool
            )
        )

    assert isinstance(captured["prompt"], str)
    assert captured["prompt"] == "hello"
    assert captured["permission_mode"] == "bypassPermissions"
