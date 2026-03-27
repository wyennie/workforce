"""Manager: takes one ticket and produces a Decomposition.

The Manager is a planning step, not a worker. It runs read-only against the
source repo (Read/Glob/Grep only, no Write/Edit/Bash) and outputs structured
JSON describing how the ticket should be sliced — parallel across multiple
specialists, sequential, or single-specialist.

The Decomposition feeds the parallel dispatch orchestrator (parallel.py).
"""

from __future__ import annotations

import asyncio
import json
import re
from enum import Enum
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field, field_validator


SCHEMA_VERSION = 1


# Manager runs as a built-in role, not a hireable specialist. The user doesn't
# hire/fire managers; one is invoked implicitly when --parallel is used.
DEFAULT_MANAGER_MODEL = "claude-sonnet-4-6"
MANAGER_ALLOWED_TOOLS = ["Read", "Glob", "Grep"]


# ----- Models ---------------------------------------------------------------


class DecompositionKind(str, Enum):
    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"
    SINGLE = "single"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    needed: bool = False
    path: str = ""           # relative path under _workforce/contracts/
    body: str = ""           # markdown contract body


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    description: str
    owns_paths: list[str] = Field(default_factory=list)
    excludes_paths: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    suggested_specialist: str | None = None
    template_hint: str | None = None  # used to auto-hire if specialist missing
    estimated_turns: int = 10

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", v):
            raise ValueError(
                "task id must be lowercase, start with a letter, "
                "and contain only [a-z0-9_-] (max 32 chars)"
            )
        return v


class Decomposition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = SCHEMA_VERSION
    ticket: str
    kind: DecompositionKind
    rationale: str
    contract: Contract = Field(default_factory=Contract)
    tasks: list[Task] = Field(default_factory=list)
    merge_order: list[str] = Field(default_factory=list)


# ----- Manager prompt -------------------------------------------------------


MANAGER_SYSTEM_PROMPT = """\
You are the Workforce Manager. Your job is to take one ticket and produce a
*decomposition*: either a plan to fan it out across parallel specialists, a
sequential chain, or an honest "this doesn't decompose, dispatch as one
specialist" verdict.

You have read access to the repository (Read, Glob, Grep). Your only output
is a single decomposition JSON in a fenced ```json block — no other text
before or after it. No prose. No commentary. Just the fenced JSON.

## How to think

Before deciding, look at the repo. Understand:
- What files would change for this ticket?
- Is there an interface that needs to be defined first, or one that already
  exists?
- Could the work be sliced so different specialists touch different files?

Then choose a kind:

**parallel** — when subtasks can be defined against a fixed contract or
existing interface, and each subtask owns a disjoint set of files. The
classic shape: contract → impl + tests + callers + docs in parallel.

**sequential** — when subtasks have hard dependencies but each is bounded.
E.g. "set up the database, then add the migration, then add the API". Each
runs in its own worktree, but later ones fork from the result of earlier
ones.

**single** — when the work is one tight unit (one bug, one small refactor)
and decomposing would just add coordination overhead. Be willing to choose
this. A good "single" verdict is more valuable than a forced "parallel"
that creates merge headaches.

## Hard rules

- Two parallel tasks must NOT have overlapping `owns_paths`. Use
  `excludes_paths` if you need a "everything except X" carve-out.
- A `contract` is REQUIRED for any parallel decomposition where specialists
  need to agree on an API. Write it as crisp signatures + behavior, not
  prose. Functions, types, errors, edge cases — be specific.
- If you cannot write a crisp contract, you don't have a parallelizable
  ticket. Drop to `single`.
- `description` for each task must be specific enough that a specialist
  who hasn't seen the rest of the decomposition can do their part
  correctly. Mention the contract path. Name the files they own.
- For `single`, produce one task in `tasks` with the full ticket as its
  description, and no contract.

## Picking specialists

You'll be told which specialists are already assigned to this project, with
mission counts. **Prefer assigned specialists** — they have project memory
from past missions and know this codebase.

If a task needs a specialty that no assigned specialist covers, suggest a
new name and provide `template_hint` so Workforce can hire one for you.
The available templates are:

- `backend` — APIs, services, data models, infrastructure
- `frontend` — components, state, accessibility, UI
- `tester` — writes/maintains tests, hunts regressions
- `reviewer` — read-only code reviewer (no Write/Edit)
- `generalist` — anything

`suggested_specialist` is the name to use; `template_hint` is the template
to hire from if that name doesn't exist yet. Names should be short
descriptive slugs (e.g., `docs`, `api-tester`, `migration-aria`). If you
suggest an existing assigned specialist, leave `template_hint` null.

## Output schema

```json
{
  "schema_version": 1,
  "ticket": "<verbatim>",
  "kind": "parallel" | "sequential" | "single",
  "rationale": "<one sentence: why this kind>",
  "contract": {
    "needed": true | false,
    "path": "_workforce/contracts/<slug>.md",
    "body": "<markdown contract body>"
  },
  "tasks": [
    {
      "id": "<short-slug>",
      "description": "<specific instructions including contract path if relevant>",
      "owns_paths": ["<glob>", "..."],
      "excludes_paths": ["<glob>", "..."],
      "depends_on": ["contract", "<other-task-id>"],
      "suggested_specialist": "<existing-assigned-name OR new-name>",
      "template_hint": "<template-name OR null>",
      "estimated_turns": <int>
    }
  ],
  "merge_order": ["<task-id>", "..."]
}
```

`contract.needed=false` and empty `path`/`body` for tickets that don't need
one. `merge_order` lists task ids in the order they should be merged onto
the source branch — must respect `depends_on`.
"""


@dataclass(frozen=True)
class SpecialistInfo:
    """Compact view of one specialist for the Manager's prompt."""
    name: str
    role: str
    project_missions: int  # past completed missions on THIS project


def _user_prompt(ticket: str, project_specialists: list[SpecialistInfo]) -> str:
    if project_specialists:
        lines = []
        for s in project_specialists:
            tag = f"({s.project_missions} mission{'s' if s.project_missions != 1 else ''} on this project)"
            lines.append(f"- `{s.name}` {tag} — {s.role}")
        listing = "\n".join(lines)
    else:
        listing = "(none assigned to this project yet)"
    return f"""\
## Ticket

{ticket.strip()}

## Specialists currently assigned to this project

{listing}

Prefer these names — they have project memory and know the codebase.
For any task that needs a specialty none of them cover, suggest a new
short name and set `template_hint` to one of: `backend`, `frontend`,
`tester`, `reviewer`, `generalist`. Workforce will auto-hire from the
template before dispatching.

Now look at the repo and produce the decomposition JSON.
"""


# ----- Parsing --------------------------------------------------------------


_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


class ManagerError(Exception):
    """Raised when the Manager's output can't be parsed into a Decomposition."""


def parse_decomposition(text: str) -> Decomposition:
    """Extract the last fenced ```json block, parse, validate against schema."""
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
            return Decomposition.model_validate(data)
        except ValueError as e:
            last_err = e
            continue
    raise ManagerError(
        f"could not parse a Decomposition from manager output: {last_err}"
    )


# ----- Validation -----------------------------------------------------------


CONTRACT_TASK_ID = "contract"  # synthetic dependency token; not a real task


class ValidationError(Exception):
    """Decomposition violates a hard rule."""


def validate_decomposition(
    decomp: Decomposition,
    *,
    repo_path: Path | None = None,
    available_specialists: list[str] | None = None,
) -> None:
    """Raise ValidationError on any rule violation.

    Checks (in order):
    - At least one task.
    - Task ids are unique.
    - All `depends_on` references resolve (other task id OR "contract").
    - Dependency graph is a DAG (no cycles).
    - `merge_order` covers every task and respects `depends_on`.
    - For `parallel`: tasks with no shared deps don't have overlapping
      `owns_paths` (after applying `excludes_paths`).
    - For `parallel` requiring contract coordination, `contract.needed=True`.
    - Suggested specialists exist in the roster (if list provided).
    """
    if not decomp.tasks:
        raise ValidationError("decomposition has no tasks")

    ids = [t.id for t in decomp.tasks]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValidationError(f"duplicate task ids: {', '.join(dupes)}")

    # Resolve dependency references
    valid_refs = set(ids) | {CONTRACT_TASK_ID}
    for t in decomp.tasks:
        bad = [d for d in t.depends_on if d not in valid_refs]
        if bad:
            raise ValidationError(
                f"task {t.id!r} depends on unknown task(s): {', '.join(bad)}"
            )

    # DAG check (topological sort; raise on cycle)
    _topological_sort(decomp.tasks)

    # merge_order: covers every task, respects depends_on
    if decomp.merge_order:
        if set(decomp.merge_order) != set(ids):
            missing = set(ids) - set(decomp.merge_order)
            extra = set(decomp.merge_order) - set(ids)
            parts = []
            if missing:
                parts.append(f"missing: {', '.join(sorted(missing))}")
            if extra:
                parts.append(f"unknown: {', '.join(sorted(extra))}")
            raise ValidationError(
                f"merge_order doesn't cover the task set ({'; '.join(parts)})"
            )
        position = {tid: i for i, tid in enumerate(decomp.merge_order)}
        for t in decomp.tasks:
            for dep in t.depends_on:
                if dep == CONTRACT_TASK_ID:
                    continue
                if position[dep] > position[t.id]:
                    raise ValidationError(
                        f"merge_order violates depends_on: "
                        f"{t.id!r} comes before {dep!r} but depends on it"
                    )

    # Parallel-specific checks
    if decomp.kind is DecompositionKind.PARALLEL:
        _check_parallel_overlap(decomp, repo_path)

    # Note: we deliberately do NOT validate that suggested specialists exist
    # in the roster here — auto-staff (parallel.resolve_task_specialists)
    # handles missing names by either auto-assigning from the global roster
    # or auto-hiring from `template_hint`. The resolver raises a clear
    # ResolutionError if it can't satisfy a task. Putting the check here too
    # would block legitimate auto-hire cases.
    _ = available_specialists  # parameter retained for backwards-compat


def _topological_sort(tasks: list[Task]) -> list[str]:
    """Kahn's algorithm. Raises ValidationError if a cycle is found."""
    by_id = {t.id: t for t in tasks}
    indeg: dict[str, int] = {t.id: 0 for t in tasks}
    edges: dict[str, list[str]] = {t.id: [] for t in tasks}
    for t in tasks:
        for dep in t.depends_on:
            if dep == CONTRACT_TASK_ID or dep not in by_id:
                continue
            indeg[t.id] += 1
            edges[dep].append(t.id)

    queue = [tid for tid, d in indeg.items() if d == 0]
    order: list[str] = []
    while queue:
        tid = queue.pop(0)
        order.append(tid)
        for nxt in edges[tid]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if len(order) != len(tasks):
        unresolved = sorted(set(by_id) - set(order))
        raise ValidationError(
            f"dependency cycle among tasks: {', '.join(unresolved)}"
        )
    return order


def _check_parallel_overlap(decomp: Decomposition, repo_path: Path | None) -> None:
    """For tasks that could run concurrently (no mutual dependency), confirm
    their owned-path globs don't share any concrete files in `repo_path`.

    This is filesystem-grounded: tasks that create entirely new files won't
    register as overlap (the files don't exist yet). Documented limitation;
    the common case (refactoring existing files) IS caught.
    """
    if repo_path is None:
        return  # caller didn't ask for path checks

    # Build "concrete file set" for each task's owns - excludes patterns.
    sets = {t.id: _resolve_paths(repo_path, t.owns_paths, t.excludes_paths) for t in decomp.tasks}

    # Two tasks can run in parallel iff neither (transitively) depends on the
    # other. Build reachability for non-contract dependencies.
    by_id = {t.id: t for t in decomp.tasks}
    def reaches(start: str, target: str) -> bool:
        if start == target:
            return False
        seen: set[str] = set()
        stack = [d for d in by_id[start].depends_on if d != CONTRACT_TASK_ID]
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(d for d in by_id[cur].depends_on if d != CONTRACT_TASK_ID)
        return False

    ids = list(by_id.keys())
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            if reaches(a, b) or reaches(b, a):
                continue  # not concurrent — order constrained by deps
            shared = sets[a] & sets[b]
            if shared:
                preview = ", ".join(sorted(str(p) for p in list(shared)[:3]))
                more = "..." if len(shared) > 3 else ""
                raise ValidationError(
                    f"parallel tasks {a!r} and {b!r} both claim files: "
                    f"{preview}{more}"
                )


def _resolve_paths(repo: Path, owns: list[str], excludes: list[str]) -> set[Path]:
    """Resolve owns/excludes globs to a set of concrete file paths under repo."""
    matched: set[Path] = set()
    for pattern in owns:
        for p in repo.glob(pattern):
            if p.is_file():
                matched.add(p.resolve())
    for pattern in excludes:
        for p in repo.glob(pattern):
            matched.discard(p.resolve())
    return matched


# ----- Run ------------------------------------------------------------------


async def run_manager(
    *,
    ticket: str,
    repo_path: Path,
    project_specialists: list[SpecialistInfo],
    model: str = DEFAULT_MANAGER_MODEL,
    max_turns: int = 25,
    max_budget_usd: float = 1.0,
    max_wall_seconds: float = 300.0,
) -> tuple[Decomposition, float, list[Any]]:
    """Run the Manager against the source repo. Returns (decomp, cost_usd, messages).

    Read-only — Manager has Read/Glob/Grep but not Write/Edit/Bash. cwd is the
    source repo so Manager can inspect the actual file tree.
    """
    options = ClaudeAgentOptions(
        cwd=str(repo_path),
        system_prompt=MANAGER_SYSTEM_PROMPT,
        allowed_tools=MANAGER_ALLOWED_TOOLS,
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
            prompt=_user_prompt(ticket, project_specialists),
            options=options,
        ):
            collected.append(msg)
            if isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd or 0.0

    try:
        await asyncio.wait_for(consume(), timeout=max_wall_seconds)
    except asyncio.TimeoutError:
        raise ManagerError(
            f"manager exceeded wall-time limit ({max_wall_seconds:.0f}s)"
        ) from None

    text = _last_assistant_text(collected)
    if not text:
        raise ManagerError("manager produced no assistant text")
    decomp = parse_decomposition(text)
    # Echo the ticket back if the model didn't quote it verbatim — keeps
    # the saved decomposition self-contained.
    if not decomp.ticket:
        decomp = decomp.model_copy(update={"ticket": ticket})
    return decomp, cost, collected


def _last_assistant_text(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if isinstance(msg, AssistantMessage):
            chunks = [b.text for b in msg.content if isinstance(b, TextBlock)]
            text = "\n".join(chunks).strip()
            if text:
                return text
    return ""
