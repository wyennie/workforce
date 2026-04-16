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
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator

from workforce.specialist import DEFAULT_MODEL
from workforce.utils import _FENCE_RE

SCHEMA_VERSION = 1


# Manager runs as a built-in role, not a hireable specialist. The user doesn't
# hire/fire managers; one is invoked implicitly when --parallel is used.
DEFAULT_MANAGER_MODEL = DEFAULT_MODEL
MANAGER_ALLOWED_TOOLS = ["Read", "Glob", "Grep"]


# ----- Models ---------------------------------------------------------------


class DecompositionKind(StrEnum):
    """How the Manager chose to decompose the ticket."""

    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"
    SINGLE = "single"


class Contract(BaseModel):
    """API contract the Manager writes for parallel sub-missions to agree on.

    When ``needed=True``, ``body`` contains the markdown contract text.
    ``path`` is the intended storage location (for reference; the parallel
    orchestrator materializes the contract under the parent mission dir).
    """

    model_config = ConfigDict(extra="forbid")
    needed: bool = False
    path: str = ""           # relative path under _workforce/contracts/
    body: str = ""           # markdown contract body


class Task(BaseModel):
    """One unit of work in a Decomposition, assigned to a single specialist.

    Attributes:
        id: Short slug used in mission ids and merge order references.
        description: Full instructions for the specialist (must be
            self-contained — the specialist won't see the other tasks).
        owns_paths: Glob patterns declaring which files this task may write.
        excludes_paths: Globs carved out of ``owns_paths`` (this task must
            not write these even if they match an ``owns_paths`` pattern).
        depends_on: Task ids that must complete before this task starts, plus
            the synthetic ``"contract"`` sentinel meaning "after the contract
            is written."
        suggested_specialist: Name of the specialist to run this task.
        template_hint: Template to hire from if ``suggested_specialist``
            doesn't exist in the roster and auto-staff is enabled.
        estimated_turns: Rough turn-count hint for UI display; not enforced.
    """

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
    """The Manager's structured plan for executing a ticket.

    Produced by :func:`run_manager` and validated by
    :func:`validate_decomposition` before any sub-missions are started.

    Attributes:
        schema_version: Forward-compat version tag.
        ticket: The original ticket text (echoed for self-contained records).
        kind: How the work is split — parallel, sequential, or single.
        rationale: One-sentence explanation of why this kind was chosen.
        contract: API contract for parallel decompositions; ``needed=False``
            for single/sequential.
        tasks: The list of tasks in dependency order.
        merge_order: Task ids in the order they should be merged into the
            source branch.
    """

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

## Shared files: one owner, never two

Some files are needed by multiple domains: `package.json`, `tsconfig.json`,
`.env.example`, `.gitignore`, `docker-compose.yml`, top-level configs,
shared `__init__.py` or `index.ts` re-export hubs. **Each such file
belongs to exactly ONE task.** Other tasks must NOT create or modify it,
even though they may need it to exist.

Concretely:
- Pick the task whose domain "owns" the file (e.g., `client/package.json`
  goes to the frontend task; `server/package.json` goes to the backend
  task; root-level `package.json` goes to whichever task sets up the
  workspace, often a separate "scaffold").
- Add the file explicitly to that task's `owns_paths`.
- In OTHER tasks' descriptions, mention "the X task owns Y; you can read
  it but do not create or modify it." This sets expectations.

If multiple tasks genuinely need to bootstrap shared infrastructure
(monorepo workspace, shared TypeScript types module, etc.), give that
infrastructure its OWN task and have the others `depends_on` it. That's
what `kind: sequential` is for. Don't try to parallelize work that needs
shared scaffolding to exist first.

The most common bug in a parallel decomposition is two specialists each
creating their own version of `package.json` (or similar). Workforce will
flag this post-hoc, but it's better to prevent it here.

## Picking specialists

You'll be told which specialists are already assigned to this project, with
mission counts and a one-line role description for each. **Match role to
task** — don't dispatch a frontend specialist to write backend tests just
because they're available. **Prefer assigned specialists with relevant
roles** — they have project memory from past missions and know this
codebase. If no assigned specialist matches the task's domain, suggest a
new one rather than misuse an existing one.

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


def _user_prompt(
    ticket: str,
    project_specialists: list[SpecialistInfo],
    *,
    prior_decomposition: Decomposition | None = None,
    user_feedback: str | None = None,
) -> str:
    """Build the Manager's user-turn prompt.

    Includes the ticket, a listing of assigned specialists, and (for
    replanning) the prior decomposition + user feedback so the Manager
    can revise its plan rather than starting from scratch.
    """
    if project_specialists:
        lines = []
        for s in project_specialists:
            tag = f"({s.project_missions} mission{'s' if s.project_missions != 1 else ''} on this project)"
            lines.append(f"- `{s.name}` {tag} — {s.role}")
        listing = "\n".join(lines)
    else:
        listing = "(none assigned to this project yet)"

    parts = [
        f"## Ticket\n\n{ticket.strip()}",
        f"## Specialists currently assigned to this project\n\n{listing}",
        (
            "Prefer these names — they have project memory and know the codebase.\n"
            "For any task that needs a specialty none of them cover, suggest a new\n"
            "short name and set `template_hint` to one of: `backend`, `frontend`,\n"
            "`tester`, `reviewer`, `generalist`. Workforce will auto-hire from the\n"
            "template before dispatching."
        ),
    ]

    if prior_decomposition is not None and user_feedback:
        parts.append(
            "## Replanning request\n\n"
            "You produced this decomposition earlier:\n\n"
            "```json\n"
            + prior_decomposition.model_dump_json(indent=2)
            + "\n```\n\n"
            "The user reviewed it and asked for changes:\n\n"
            "> " + user_feedback.strip().replace("\n", "\n> ") + "\n\n"
            "Produce a REVISED decomposition addressing this feedback. "
            "Keep what worked; change what they pointed at. Same output schema."
        )
    else:
        parts.append("Now look at the repo and produce the decomposition JSON.")

    return "\n\n".join(parts)


# ----- Parsing --------------------------------------------------------------


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

    `available_specialists` is accepted for backwards compatibility but is NOT
    checked here. Specialist existence is intentionally deferred to
    `parallel.resolve_task_specialists`, which handles auto-assignment and
    auto-hiring from templates. Validating here would reject legitimate
    auto-hire cases before the resolver gets a chance to satisfy them.
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

    queue: deque[str] = deque(tid for tid, d in indeg.items() if d == 0)
    order: list[str] = []
    while queue:
        tid = queue.popleft()
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
    their owned-path lanes don't overlap.

    Two checks compose:
    1. **Pattern overlap** — do the glob patterns themselves share any
       theoretical path? Catches "tasks A and B both claim ``outputs/*.json``"
       even in an empty workspace dir where no files exist yet.
    2. **File-set overlap** — when a real `repo_path` is given, also intersect
       the resolved file sets. Catches concrete cases the user can act on
       (here are the specific files both tasks would touch).

    Pattern overlap covers the soundness-critical cases (especially in
    workspace dirs that start sparse); file-set overlap gives the user a
    sharper error message when files exist.
    """
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

    # Pre-resolve each task's owns/excludes to concrete file sets when we have
    # a repo path. Empty when repo_path is None.
    file_sets: dict[str, set[Path]] = (
        {t.id: _resolve_paths(repo_path, t.owns_paths, t.excludes_paths) for t in decomp.tasks}
        if repo_path is not None else
        {t.id: set() for t in decomp.tasks}
    )

    ids = list(by_id.keys())
    for i, a in enumerate(ids):
        ta = by_id[a]
        if not ta.owns_paths:
            continue  # nothing declared → no claim to compare against
        for b in ids[i + 1 :]:
            if reaches(a, b) or reaches(b, a):
                continue  # not concurrent — order constrained by deps
            tb = by_id[b]
            if not tb.owns_paths:
                continue

            # Pattern overlap (sound regardless of filesystem state).
            overlapping = _pattern_overlap_pair(ta, tb)
            if overlapping:
                raise ValidationError(
                    f"parallel tasks {a!r} and {b!r} have overlapping path lanes: "
                    f"{overlapping[0]!r} (from {a}) and {overlapping[1]!r} (from {b}) "
                    "can both match the same path. Tighten one or split the work."
                )

            # File-set overlap (sharper error, only when a repo path is given).
            shared = file_sets[a] & file_sets[b]
            if shared:
                preview = ", ".join(sorted(str(p) for p in list(shared)[:3]))
                more = "..." if len(shared) > 3 else ""
                raise ValidationError(
                    f"parallel tasks {a!r} and {b!r} both claim files: "
                    f"{preview}{more}"
                )


def _pattern_overlap_pair(a: Task, b: Task) -> tuple[str, str] | None:
    """Find any (owns_a, owns_b) pair whose glob patterns can match a common
    path that isn't carved out by either side's excludes_paths.

    Returns the overlapping pattern pair for the error message, or None.
    """
    from workforce.globmatch import globs_overlap

    for pa in a.owns_paths:
        for pb in b.owns_paths:
            if not globs_overlap(pa, pb):
                continue
            # Patterns share at least one path. If either side carves the
            # OTHER task's claim out via its own excludes, the lanes are still
            # disjoint in practice. We only suppress overlap when the carve-out
            # is structurally clear (exact match or **-superset) so we don't
            # silently miss real conflicts.
            if _exclude_covers(pb, a.excludes_paths):
                continue
            if _exclude_covers(pa, b.excludes_paths):
                continue
            return (pa, pb)
    return None


def _exclude_covers(owns_pattern: str, excludes: list[str]) -> bool:
    """True if every path matching `owns_pattern` is shadowed by some pattern
    in `excludes`. Conservative — only catches the obvious cases:

    - exact equality (``excludes`` contains ``owns_pattern`` literally), or
    - a ``**``-suffixed pattern in ``excludes`` whose prefix is a prefix of
      ``owns_pattern`` (e.g. ``owns="app/legacy/v1.py"`` is covered by
      ``excludes="app/legacy/**"``), or
    - a ``**/``-prefixed pattern in ``excludes`` whose suffix matches
      ``owns_pattern`` exactly or as a path tail.

    Anything more clever risks false negatives (declaring "covered" when it
    isn't and silently dropping a real overlap). Erring conservative means
    the user might see a false-positive overlap warning if they construct an
    unusually-shaped excludes — they can rephrase to a clearer form.
    """
    for ex in excludes:
        if ex == owns_pattern:
            return True
        if ex.endswith("/**"):
            prefix = ex[:-3]
            if owns_pattern == prefix or owns_pattern.startswith(prefix + "/"):
                return True
        if ex.startswith("**/"):
            suffix = ex[3:]
            if owns_pattern == suffix or owns_pattern.endswith("/" + suffix):
                return True
    return False


def _glob_files(repo: Path, pattern: str) -> Iterator[Path]:
    """Yield files matching `pattern` under `repo`.

    On Python <3.13, ``Path.glob("src/**")`` returns only directories, while on
    3.13+ it also returns files. Normalise both: if the glob hits a directory,
    walk into it and yield its files.
    """
    for p in repo.glob(pattern):
        if p.is_file():
            yield p
        elif p.is_dir():
            for sub in p.rglob("*"):
                if sub.is_file():
                    yield sub


def _resolve_paths(repo: Path, owns: list[str], excludes: list[str]) -> set[Path]:
    """Resolve owns/excludes globs to a set of concrete file paths under repo."""
    matched: set[Path] = set()
    for pattern in owns:
        for p in _glob_files(repo, pattern):
            matched.add(p.resolve())
    for pattern in excludes:
        for p in _glob_files(repo, pattern):
            matched.discard(p.resolve())
    return matched


# ----- Post-mission ownership audit -----------------------------------------


def audit_ownership(
    worktree: Path,
    base_sha: str,
    owns_paths: list[str],
    excludes_paths: list[str],
) -> list[str]:
    """Return files this sub-mission touched that aren't in its declared lane.

    Compares files changed in `base_sha..HEAD` against the union of `owns_paths`
    minus `excludes_paths` (resolved as globs against the worktree). Empty list
    means the specialist stayed in their lane.

    Empty `owns_paths` means everything is out-of-lane (the Manager forgot to
    declare ownership). Empty changed set means nothing to audit.
    """
    import subprocess as _sp
    out = _sp.run(
        ["git", "diff", "--name-only", f"{base_sha}..HEAD"],
        cwd=worktree, capture_output=True, text=True, check=True,
    ).stdout
    changed = [line for line in out.splitlines() if line.strip()]
    if not changed:
        return []

    if not owns_paths:
        # Nothing declared as owned → everything written is out-of-lane.
        return sorted(changed)

    in_lane: set[str] = set()
    for pattern in owns_paths:
        for p in _glob_files(worktree, pattern):
            try:
                in_lane.add(str(p.relative_to(worktree)))
            except ValueError:
                continue
    for pattern in excludes_paths:
        for p in _glob_files(worktree, pattern):
            try:
                in_lane.discard(str(p.relative_to(worktree)))
            except ValueError:
                continue
    return sorted(f for f in changed if f not in in_lane)


# ----- Run ------------------------------------------------------------------


async def run_manager(
    *,
    ticket: str,
    repo_path: Path,
    project_specialists: list[SpecialistInfo],
    prior_decomposition: Decomposition | None = None,
    user_feedback: str | None = None,
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
            prompt=_user_prompt(
                ticket, project_specialists,
                prior_decomposition=prior_decomposition,
                user_feedback=user_feedback,
            ),
            options=options,
        ):
            collected.append(msg)
            if isinstance(msg, ResultMessage):
                cost = msg.total_cost_usd or 0.0

    try:
        await asyncio.wait_for(consume(), timeout=max_wall_seconds)
    except TimeoutError:
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
