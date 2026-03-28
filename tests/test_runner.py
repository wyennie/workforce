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
