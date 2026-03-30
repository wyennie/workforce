"""Reviewer: gates a sub-mission's completion.

After a worker finishes their work, the Reviewer reads the diff, the
original ticket, and (optionally) the contract, then decides:
  - approve: the work is good, mission is done.
  - request revisions: the work has issues; the worker should try again.

Built-in role like the Manager — not a hireable specialist. Read-only
tools (Read, Glob, Grep, Bash for running tests). Returns structured JSON.

Used by mission.dispatch in a revision loop: worker runs → Reviewer
checks → if rejected, worker re-runs with feedback → ... up to N rounds.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1


DEFAULT_REVIEWER_MODEL = "claude-sonnet-4-6"
REVIEWER_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Bash"]


# ----- Models ---------------------------------------------------------------


class Review(BaseModel):
    """One Reviewer verdict on a worker's output."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SCHEMA_VERSION
    approved: bool
    summary: str = ""
    issues: list[str] = Field(default_factory=list)


class ReviewError(Exception):
    """Reviewer output couldn't be parsed into a Review."""


# ----- Reviewer prompt ------------------------------------------------------


REVIEWER_SYSTEM_PROMPT = """\
You are the Workforce Reviewer. Your job is to review one specialist's
work — not to write code, not to redesign the approach, just to verdict.

The work has been committed to a git branch in the worktree you're sitting
in. Your tools are read-only (Read, Glob, Grep) plus Bash for running
tests / linters / type checkers — anything that grounds your review in
something more than diff-staring.

Your output is a single JSON object in a fenced ```json block. No prose
before or after. No commentary outside the JSON.

## How to decide

You're checking three things, in order:

1. **Does the work match the ticket?** Read the ticket. Read the diff.
   Did the specialist actually do what was asked, or did they go
   sideways?

2. **Does it work?** If there are tests, run them. If the project is
   typed (TypeScript, Python with mypy), run the type checker. If it's
   a build that should compile, try compiling. Don't trust the diff
   alone — verify.

3. **Are there obvious problems a reviewer would flag?** Bugs, missing
   error handling at boundaries, missing tests for tricky logic, broken
   contract compliance. Be specific. Cite lines or files.

Be concise. Don't list nits like "this could be slightly more elegant"
or "consider adding a comment here." Reviewers who do that waste
specialists' time. Flag substantive issues only.

## When to approve vs request revisions

**approve** when:
- The work matches the ticket.
- It runs / compiles / tests pass (or there are no tests/build to run).
- No substantive issues.

**request revisions** when:
- The work is incomplete or doesn't match the ticket.
- Tests fail, type checker complains, build is broken.
- There's a real bug, missing error handling, or contract violation.

Do NOT request revisions for:
- Style preferences ("I'd name this differently")
- Hypothetical futures ("what if we later need...")
- Nice-to-haves not in the ticket

## Output schema

```json
{
  "schema_version": 1,
  "approved": true | false,
  "summary": "<one-sentence verdict>",
  "issues": ["<specific issue with file:line if relevant>", "..."]
}
```

`issues` is empty when approved. When requesting revisions, list each
distinct issue separately so the specialist can address them.
"""


def _user_prompt(
    ticket: str,
    base_sha: str,
    contract: str | None,
    prior_reviews: list[Review],
) -> str:
    parts: list[str] = [
        f"## Ticket\n\n{ticket.strip()}",
        f"## Base commit\n\n`{base_sha}` — diff this against HEAD to see what was added.",
    ]
    if contract and contract.strip():
        parts.append(
            "## Contract the specialist agreed to\n\n"
            "```\n"
            + contract.strip()
            + "\n```"
        )
    if prior_reviews:
        history = []
        for i, r in enumerate(prior_reviews, 1):
            history.append(
                f"### Round {i}\n"
                f"approved={r.approved}\n"
                f"summary: {r.summary}\n"
                + ("issues:\n" + "\n".join(f"- {iss}" for iss in r.issues) if r.issues else "")
            )
        parts.append(
            "## Prior reviews on earlier rounds\n\n"
            "The specialist has been through this loop before. Check whether "
            "they actually addressed the prior round's issues.\n\n"
            + "\n\n".join(history)
        )
    parts.append(
        "## What to do\n\n"
        "Use Read/Glob/Grep to inspect the diff. Use Bash to run tests, "
        "the type checker, or the build if applicable. Then output your "
        "verdict JSON."
    )
    return "\n\n".join(parts)


# ----- Parsing --------------------------------------------------------------


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def parse_review(text: str) -> Review:
    """Extract the last fenced ```json block, parse, validate."""
    matches = _FENCE_RE.findall(text)
    candidates = matches if matches else [text]

    last_err: Exception | None = None
    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            last_err = e
            continue
        try:
            return Review.model_validate(data)
        except ValueError as e:
            last_err = e
            continue
    raise ReviewError(f"could not parse a Review from reviewer output: {last_err}")


# ----- Run ------------------------------------------------------------------


async def run_reviewer(
    *,
    worktree_path: Path,
    base_sha: str,
    ticket: str,
    contract: str | None = None,
    prior_reviews: list[Review] | None = None,
    model: str = DEFAULT_REVIEWER_MODEL,
    max_turns: int = 30,
    max_budget_usd: float = 1.0,
    max_wall_seconds: float = 300.0,
) -> tuple[Review, float]:
    """Run the Reviewer against the worktree's diff. Returns (review, cost_usd)."""
    options = ClaudeAgentOptions(
        cwd=str(worktree_path),
        system_prompt=REVIEWER_SYSTEM_PROMPT,
        allowed_tools=REVIEWER_ALLOWED_TOOLS,
        model=model,
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        permission_mode="bypassPermissions",
    )

    collected: list[Any] = []
    cost = 0.0

    async def consume() -> None:
        nonlocal cost
        async for msg in query(
            prompt=_user_prompt(ticket, base_sha, contract, prior_reviews or []),
            options=options,
        ):
            collected.append(msg)
            if isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd or 0.0

    try:
        await asyncio.wait_for(consume(), timeout=max_wall_seconds)
    except TimeoutError:
        raise ReviewError(
            f"reviewer exceeded wall-time limit ({max_wall_seconds:.0f}s)"
        ) from None

    text = _last_assistant_text(collected)
    if not text:
        raise ReviewError("reviewer produced no assistant text")
    return parse_review(text), cost


def _last_assistant_text(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, AssistantMessage):
            chunks = [b.text for b in msg.content if isinstance(b, TextBlock)]
            text = "\n".join(chunks).strip()
            if text:
                return text
    return ""


# ----- Diff stats helper for status display ---------------------------------


def diff_stats(worktree: Path, base_sha: str) -> tuple[int, int, int]:
    """(files_changed, insertions, deletions) for `base_sha..HEAD`. Used for
    the CLI summary so the user sees what the Reviewer was looking at.
    """
    try:
        out = subprocess.run(
            ["git", "diff", "--shortstat", f"{base_sha}..HEAD"],
            cwd=worktree, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return (0, 0, 0)
    # "3 files changed, 47 insertions(+), 12 deletions(-)"
    files = re.search(r"(\d+) files? changed", out)
    ins = re.search(r"(\d+) insertions?", out)
    dels = re.search(r"(\d+) deletions?", out)
    return (
        int(files.group(1)) if files else 0,
        int(ins.group(1)) if ins else 0,
        int(dels.group(1)) if dels else 0,
    )
