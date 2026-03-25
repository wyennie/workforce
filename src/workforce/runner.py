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
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

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


class RunStatus(str, Enum):
    COMPLETED = "completed"     # ResultMessage arrived, is_error == False
    ERROR = "error"             # SDK reported an error or no ResultMessage
    WALL_TIMEOUT = "wall_timeout"
    INTERRUPTED = "interrupted"


@dataclass
class RunResult:
    status: RunStatus
    final: ResultMessage | None
    cost_usd: float
    duration_seconds: float
    turn_count: int
    error_detail: str | None = None


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
) -> RunResult:
    """Run one mission to completion (or limit) and return the result.

    Streams every SDK message both to `events_log` (JSONL, one record per line,
    flushed immediately) and to `on_message(msg)` if provided. The two channels
    are independent: the log is for replay, the callback is for live UI.
    """
    limits = limits or RunLimits()
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        system_prompt=system_prompt,
        allowed_tools=list(spec.allowed_tools),
        model=spec.model,
        max_turns=limits.max_turns,
        max_budget_usd=limits.max_budget_usd,
        permission_mode="bypassPermissions",
    )

    started = time.monotonic()
    state: _RunState = _RunState()
    log_fh = events_log.open("w") if events_log else None

    async def consume() -> None:
        async for msg in query(prompt=user_prompt, options=options):
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
        except asyncio.TimeoutError:
            return _make_result(
                state,
                started,
                RunStatus.WALL_TIMEOUT,
                f"wall time exceeded ({limits.max_wall_seconds:.0f}s)",
            )
        except asyncio.CancelledError:
            return _make_result(state, started, RunStatus.INTERRUPTED, "cancelled")
    finally:
        if log_fh is not None:
            log_fh.close()

    final = state.final
    if final is None:
        return _make_result(
            state, started, RunStatus.ERROR, "session ended without ResultMessage"
        )
    if final.is_error:
        detail = "; ".join(final.errors) if final.errors else "is_error=True"
        return _make_result(state, started, RunStatus.ERROR, detail)
    return _make_result(state, started, RunStatus.COMPLETED, None)


@dataclass
class _RunState:
    final: ResultMessage | None = None


def _make_result(
    state: _RunState,
    started: float,
    status: RunStatus,
    detail: str | None,
) -> RunResult:
    final = state.final
    return RunResult(
        status=status,
        final=final,
        cost_usd=(final.total_cost_usd or 0.0) if final else 0.0,
        duration_seconds=time.monotonic() - started,
        turn_count=final.num_turns if final else 0,
        error_detail=detail,
    )
