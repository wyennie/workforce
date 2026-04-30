"""Specialist runner.

Spawns one `claude_agent_sdk.query()` session in a worktree, streams events,
enforces limits, and returns a structured result. The orchestrator composes
the prompts and decides what to do with the result; this module just runs the
session cleanly.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    query,
)

from workforce.specialist import Specialist


@dataclass
class RunLimits:
    """Hard caps on a mission. The first one to trip stops the run."""
    max_turns: int = 50
    max_budget_usd: float = 5.0
    max_wall_seconds: float = 1800.0  # 30 min
    max_retries: int = 0
    retry_backoff_base: float = 30.0


class RunStatus(StrEnum):
    """Terminal state of a specialist run."""

    COMPLETED = "completed"     # ResultMessage arrived, is_error == False
    ERROR = "error"             # SDK reported an error or no ResultMessage
    WALL_TIMEOUT = "wall_timeout"
    INTERRUPTED = "interrupted"


@dataclass
class RunResult:
    """Everything the orchestrator needs to know about a completed run.

    Attributes:
        status: Final state of the run.
        final: The SDK's ResultMessage, if one was received before the run
            ended (None on timeout or SDK crash before the SDK could send it).
        cost_usd: Total API cost in USD from the ResultMessage, or 0.0 if
            the run ended without one.
        duration_seconds: Wall-clock time from start to end.
        turn_count: Number of assistant turns from the ResultMessage.
        error_detail: Human-readable description of the failure, or None
            when status is COMPLETED.
    """

    status: RunStatus
    final: ResultMessage | None
    cost_usd: float
    duration_seconds: float
    turn_count: int
    error_detail: str | None = None


#: Callback invoked with each SDK message as it streams in. Used by the CLI
#: to render live output and by the orchestrator to collect messages for the
#: transcript.
EventCallback = Callable[[Any], None]


def message_to_jsonable(msg: Any) -> dict[str, Any]:
    """Serialize one SDK message to a plain dict for JSONL logging.

    SDK messages and content blocks are dataclasses — `asdict` handles nesting.
    We tag each record with `_type` so consumers can branch on the class name.
    """
    if dataclasses.is_dataclass(msg) and not isinstance(msg, type):
        d = dataclasses.asdict(msg)
    else:
        d = {"repr": repr(msg)}
    d["_type"] = type(msg).__name__
    return d


async def run_specialist(
    *,
    spec: Specialist,
    system_prompt: str,
    user_prompt: str,
    cwd: Path,
    limits: RunLimits | None = None,
    events_log: Path | None = None,
    on_message: EventCallback | None = None,
    can_use_tool: Any = None,
) -> RunResult:
    """Run one mission to completion (or limit) and return the result.

    Streams every SDK message both to `events_log` (JSONL, one record per line,
    flushed immediately) and to `on_message(msg)` if provided. The two channels
    are independent: the log is for replay, the callback is for live UI.

    `can_use_tool` is an optional async callback the SDK calls before each tool
    use; mission orchestrators use it to enforce path-ownership lanes for
    parallel sub-missions. See `workforce.permissions`.
    """
    limits = limits or RunLimits()
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        system_prompt=system_prompt,
        allowed_tools=list(spec.allowed_tools),
        model=spec.model,
        max_turns=limits.max_turns,
        max_budget_usd=limits.max_budget_usd,
        # `can_use_tool` requires permission_mode="default" per the SDK contract;
        # bypass mode short-circuits the callback. With a callback set we also
        # don't want auto-bypass, since the callback IS the authority.
        permission_mode="default" if can_use_tool is not None else "bypassPermissions",
        can_use_tool=can_use_tool,
    )

    started = time.monotonic()
    state: _RunState = _RunState()
    log_fh = events_log.open("w") if events_log else None

    # Capture the underlying claude CLI's stderr alongside the event log so
    # we have real diagnostics when the SDK reports a generic failure.
    stderr_path = events_log.with_name("stderr.log") if events_log else None
    stderr_fh = stderr_path.open("w") if stderr_path else None

    def _stderr(line: str) -> None:
        if stderr_fh is not None:
            stderr_fh.write(line + "\n")
            stderr_fh.flush()

    options.stderr = _stderr if stderr_fh is not None else None

    # The SDK's `can_use_tool` callback requires the streaming-input protocol:
    # the prompt must be an AsyncIterable of message dicts, not a bare string.
    # When no callback is set we keep the simpler string-prompt path so the
    # rest of the test suite and existing call sites are unaffected.
    prompt: str | AsyncIterable[dict[str, Any]]
    if can_use_tool is not None:
        prompt = _single_message_stream(user_prompt)
    else:
        prompt = user_prompt

    async def consume() -> None:
        async for msg in query(prompt=prompt, options=options):
            if log_fh is not None:
                log_fh.write(json.dumps(message_to_jsonable(msg), default=str) + "\n")
                log_fh.flush()
            if on_message is not None:
                on_message(msg)
            if isinstance(msg, ResultMessage):
                state.final = msg

    try:
        try:
            await asyncio.wait_for(consume(), timeout=limits.max_wall_seconds)
        except TimeoutError:
            return _make_result(
                state,
                started,
                RunStatus.WALL_TIMEOUT,
                f"wall time exceeded ({limits.max_wall_seconds:.0f}s)",
            )
        except asyncio.CancelledError:
            return _make_result(state, started, RunStatus.INTERRUPTED, "cancelled")
        except Exception as e:
            # SDK or underlying `claude` CLI failure. Don't propagate — that
            # would kill sibling sub-missions in a parallel dispatch. Mark
            # this run as errored; the orchestrator decides what to do.
            stderr_hint = (
                f" (see {stderr_path} for details)" if stderr_path else ""
            )
            return _make_result(
                state,
                started,
                RunStatus.ERROR,
                f"sdk error: {type(e).__name__}: {str(e)[:300]}{stderr_hint}",
            )
    finally:
        if log_fh is not None:
            log_fh.close()
        if stderr_fh is not None:
            stderr_fh.close()

    final = state.final
    if final is None:
        return _make_result(
            state, started, RunStatus.ERROR, "session ended without ResultMessage"
        )
    if final.is_error:
        detail = "; ".join(final.errors) if final.errors else "is_error=True"
        return _make_result(state, started, RunStatus.ERROR, detail)
    return _make_result(state, started, RunStatus.COMPLETED, None)


# TODO: Replace _single_message_stream with a public SDK API once
# claude_agent_sdk exposes one. Currently we construct the streaming-input
# message-dict format by hand (see claude_agent_sdk._internal.client ~209-215).
# Any SDK internal refactor can silently break the can_use_tool enforcement
# path. The regression test tests/test_runner.py::test_single_message_stream_shape
# documents the expected format and will fail loudly if the shape changes.
async def _single_message_stream(text: str) -> AsyncIterator[dict[str, Any]]:
    """Wrap a single user prompt as the streaming-protocol message dict.

    Matches the format the SDK uses internally when handed a string prompt
    (see claude_agent_sdk._internal.client lines ~209-215). One message in,
    then the iterable closes — equivalent to a one-shot turn.
    """
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }


@dataclass
class _RunState:
    """Mutable accumulator for the single ResultMessage from an SDK session."""

    final: ResultMessage | None = None


def _make_result(
    state: _RunState,
    started: float,
    status: RunStatus,
    detail: str | None,
) -> RunResult:
    """Build a RunResult from accumulated state and a final status."""
    final = state.final
    return RunResult(
        status=status,
        final=final,
        cost_usd=(final.total_cost_usd or 0.0) if final else 0.0,
        duration_seconds=time.monotonic() - started,
        turn_count=final.num_turns if final else 0,
        error_detail=detail,
    )
