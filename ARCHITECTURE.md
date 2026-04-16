# Workforce Architecture

> Written from a full read of the source (May 2026). Covers the current state
> of the codebase; refer to git log for historical context.

---

## System Overview

Workforce is a CLI tool that maintains a persistent **roster** of named AI
specialists (Claude-based agents) and dispatches them on tickets across
registered projects.

The core loop is:

```
ticket → Manager (plans) → Decomposition → Specialists (work) → branches → merge
```

A specialist is a named persona with a role, a base system prompt, an allowed
tool set, and two layers of memory (cross-project and per-project). Projects can
be git repos ("repo" kind) or plain working directories ("workspace" kind).
Repo-kind missions run in isolated git worktrees on `workforce/*` branches;
workspace-kind missions run directly in the project directory.

The tool wraps the `claude_agent_sdk` Python package, which itself shells out to
the `claude` CLI binary. Every user-visible output goes through `output.py`
(Rich-backed), and all on-disk state lives under `WORKFORCE_HOME`
(default `~/.workforce`).

---

## Filesystem Layout

```
~/.workforce/                       # WORKFORCE_HOME; overridable via env var
├── config.toml                     # (reserved; not yet used)
├── roster/
│   └── <specialist-name>/
│       ├── specialist.toml         # Specialist model (TOML)
│       ├── memory.md               # cross-project memory, append-only
│       └── stats.json              # SpecialistStats (missions/cost/duration)
└── projects/
    └── <12-hex-project-id>/        # SHA-256 of absolute repo path, first 12 chars
        ├── project.toml            # Project model (TOML)
        ├── memory/
        │   └── <specialist>.md     # per-specialist per-project memory
        ├── missions/
        │   └── <mission-id>/       # m-YYYYMMDD-HHMMSS-xxxx
        │       ├── ticket.md       # raw ticket text
        │       ├── events.jsonl    # every SDK message, one JSON per line
        │       ├── result.md       # final specialist summary
        │       ├── transcript.md   # human-readable assistant turns
        │       ├── meta.json       # MissionMeta or ParallelMissionMeta
        │       ├── decomposition.json  # Manager output (parallel/single)
        │       ├── stderr.log      # claude CLI stderr for diagnostics
        │       └── contract/
        │           └── contract.md # API contract materialized from Decomposition
        └── worktrees/
            └── <mission-id>/       # git worktree; absent for workspace projects
```

Repo-side marker file:
```
<repo-root>/.workforce-project-id   # 12-hex id written at `project init` time
```

The marker file survives repo moves — if the user moves the repo, the id
derived from the absolute path changes but the marker still resolves the right
project record.

---

## Key Abstractions

### `specialist.py` — Specialist data model and roster store

**`Specialist`** (Pydantic model)
- Fields: `name`, `role`, `model` (default `"claude-sonnet-4-6"`), `allowed_tools`, `base_prompt`
- Built from templates (`Specialist.from_template`) or fully custom (`Specialist.custom`)
- `common_preamble()` generates the shared commit-policy / working-style preamble baked into every specialist's base prompt

**`DEFAULT_MODEL = "claude-sonnet-4-6"`**
- Canonical constant in `specialist.py`
- Imported by `manager.py` and `reviewer.py` so model selection stays in one place

**`SpecialistStats`** — per-specialist aggregate: missions completed/failed, total cost, total duration

**`RosterStore`** — file-backed CRUD over `~/.workforce/roster/`
- No caching; re-reads from disk on every call (roster sizes are tiny)
- `append_memory()` uses `fcntl.flock(LOCK_EX)` on Unix; the fcntl import is
  guarded with a try/except for Windows compatibility (`_fcntl = None` fallback)

**Templates** — 5 built-in specialist templates:
| Template | Role | Tools |
|---|---|---|
| `backend` | Senior backend engineer | Read, Write, Edit, Bash, Glob, Grep, WebFetch |
| `frontend` | Senior frontend engineer | same |
| `tester` | Test engineer | Read, Write, Edit, Bash, Glob, Grep |
| `reviewer` | Code reviewer (read-only) | Read, Bash, Glob, Grep |
| `generalist` | Generalist engineer | same as backend |

---

### `project.py` — Project model and store

**`Project`** (Pydantic model)
- Fields: `id` (12-hex), `name`, `repo_path`, `kind` (`"repo"` | `"workspace"`), `assigned_specialists`, `default_model`
- `kind="repo"`: git work tree; missions run in per-mission worktrees with commit-cadence rules
- `kind="workspace"`: plain working directory; missions run there directly with no git operations

**`ProjectStore`** — file-backed CRUD over `~/.workforce/projects/`
- Resolves by full 12-hex id or by display name (case-insensitive, must be unique)

**Project ID resolution** (`resolve_project_id`)
1. Read `.workforce-project-id` marker if present
2. Fall back to SHA-256 of absolute path (first 12 hex chars)

---

### `mission.py` — Mission orchestrator

The layer the CLI's `dispatch` command calls. It composes prompts, manages
the worktree+runner lifecycle, extracts a memory delta, and writes all artifacts.

**`MissionStatus`** (StrEnum)
- `COMPLETED`, `ERROR`, `WALL_TIMEOUT`, `INTERRUPTED`, `REVIEW_REJECTED`

**`MissionMeta`** (Pydantic) — comprehensive on-disk record:
- IDs: `mission_id`, `project_id`, `project_name`, `specialist`, `model`
- Git context: `branch`, `worktree_path`, `base_sha` (None for workspace kind)
- Timing: `started_at`, `ended_at`, `duration_seconds`
- Cost: `cost_usd` (total), `manager_cost_usd`, `review_cost_usd`
- Content: `commits`, `reviews`, `revision_rounds`, `memory_delta_captured`

**`_write_meta(path, content)`** — atomic write via `path.with_suffix(".tmp")` then `os.replace`. Prevents a mid-write meta.json from being read as corrupt by concurrent watchers.

**`dispatch()`** (async) — runs one mission end-to-end:
1. Load memories; compose system prompt
2. Create worktree (repo kind) or use project dir (workspace kind)
3. Merge any dependency branches (`additional_merges`)
4. Call `runner.run_specialist()` (with optional path-ownership callback)
5. Optional Reviewer loop (`--review`): Reviewer checks diff → if rejected, specialist re-runs with feedback, up to `max_revisions` rounds
6. Scan commits (`scan_commits`)
7. Extract memory delta (extra SDK turn)
8. Write transcript, result.md
9. Append memory deltas (cross-project and per-project, both with fcntl locking)
10. Write meta.json atomically; update specialist stats

**Memory delta extraction** — after a successful run, one extra SDK turn (no tools, max 1 turn) asks the specialist to produce:
```json
{
  "summary": "...",
  "project_memory": "...",
  "cross_project_memory": "..."
}
```
Parsed via `_FENCE_RE` (shared from `utils.py`).

**`scan_commits()`** — uses NUL-delimited `git log --format` to list commits ahead of `base_sha`. No trailer checking; format is purely for recording the mission's output.

**`_append_project_memory()`** — appends to `projects/<id>/memory/<specialist>.md` with `fcntl.flock(LOCK_EX)`. Note: this function imports fcntl directly (not guarded), making it Linux/macOS-only in practice.

---

### `worktree.py` — Git worktree management

**`WorktreeManager`** — stateless per-project worktree CRUD:
- `create()`: validates repo is clean (staged/modified files forbidden; untracked OK), creates a new `workforce/<mission-id>` branch, adds a linked worktree under `projects/<id>/worktrees/<mission-id>/`
- `remove()` / `prune()`: cleanup via `git worktree remove` then filesystem
- Worktrees live far from the user's source tree to avoid editor file-watcher pollution

**`WorktreeRef`** — frozen dataclass: `repo_path`, `worktree_path`, `branch`, `mission_id`, `base_sha`

**Public helpers** (moved from `parallel.py`):
- `current_branch(repo_path)` → str | None — calls `git symbolic-ref --quiet --short HEAD`
- `is_clean(repo_path)` → bool — tolerates untracked files (`??` porcelain lines)

Branch naming: `workforce/<mission-id>` (prefix `"workforce/"`, defined as `BRANCH_PREFIX`).

---

### `manager.py` — Planning agent (Manager)

The Manager is a built-in read-only role (not hireable). It runs against the
source repo with `Read`, `Glob`, `Grep` only, and outputs a structured
`Decomposition` JSON.

**`Decomposition`** (Pydantic model):
- `kind`: `"parallel"` | `"sequential"` | `"single"`
- `rationale`: one sentence explaining the choice
- `contract`: optional API contract body for parallel decompositions
- `tasks`: list of `Task` objects
- `merge_order`: task ids in merge sequence

**`Task`** (Pydantic model):
- `id`, `description`, `owns_paths`, `excludes_paths`, `depends_on`
- `suggested_specialist`, `template_hint`, `estimated_turns`

**`validate_decomposition()`** — checks:
- At least one task; unique task ids
- `depends_on` references resolve (task id or synthetic `"contract"` token)
- No dependency cycles (Kahn's topological sort)
- `merge_order` covers all tasks and respects `depends_on`
- For `parallel`: no overlapping `owns_paths` between concurrent tasks (pattern-level AND file-set-level checks)

**`audit_ownership()`** — post-mission check: which files changed by a sub-mission fall outside its declared `owns_paths` lane.

**`run_manager()`** (async) — runs the Manager session; returns `(Decomposition, cost_usd, messages)`. The Manager prompt gives the full output schema and hard rules about path ownership, specialist selection, and when to choose each kind.

**Model**: `DEFAULT_MANAGER_MODEL = DEFAULT_MODEL` (imported from `specialist.py`).

---

### `parallel.py` — Parallel mission orchestration

**`dispatch_parallel()`** (async) — main entry point for multi-task dispatch:
1. Run Manager (or use `decomposition_override`)
2. Persist `decomposition.json`
3. Validate decomposition
4. Resolve specialists (`resolve_task_specialists`)
5. Confirm with user (optional)
6. Materialize contract artifact to disk
7. Fan out sub-missions wave-by-wave
8. Audit path ownership for completed sub-missions
9. Write final `ParallelMissionMeta` atomically

**`manager_cost_usd`** is passed as an explicit parameter to `dispatch_parallel`
(previously mutated post-hoc). This ensures the cost is correct in the parent
meta even when the orchestrator re-runs Manager for replanning.

**`resolve_task_specialists()`** — maps each `Task` to a `Specialist` with 6-priority logic:
1. `suggested_specialist` already assigned to project → use them
2. In global roster but not assigned + `auto_staff` → assign and use
3. Doesn't exist + `template_hint` + `auto_staff` → hire from template, assign, use
4. `fallback_specialist` provided → use it
5. Project has exactly one assigned specialist → use them
6. `ResolutionError`

**`_topological_waves()`** — groups tasks into waves that can run in parallel
respecting `depends_on`. Wave 0 = root tasks; Wave N+1 = tasks whose all real
deps are in earlier waves. Each wave is dispatched as `asyncio.gather`.

**`_write_meta()`** — same atomic-write helper as in `mission.py` (both copies
use the same `tmp + os.replace` idiom).

**`ParallelStatus`** (StrEnum):
`PLANNED` → `DISPATCHED` → `COMPLETED` | `PARTIAL` | `FAILED` | `CANCELLED`

**`ConfirmCallback`** — `(Decomposition, [(task_id, specialist_name, staffing_action)]) → bool`

**Auto-merge** — `auto_merge()` runs `git merge --no-ff` for each step in
`merge_order`. On first conflict: captures conflicting files (before abort),
aborts the merge, and marks remaining steps as skipped. `auto_merge_into()`
adds a preflight to switch to the target branch first.

---

### `reviewer.py` — Post-mission code reviewer

Built-in read-only + Bash role (runs tests, type checkers). Not hireable.

**`Review`** (Pydantic): `approved` (bool), `summary`, `issues` (list[str])

**`run_reviewer()`** (async) — given the worktree, `base_sha`, ticket, optional
contract, and prior review history, returns `(Review, cost_usd)`. The Reviewer
diffs `base_sha..HEAD` and runs grounding checks (tests, linters) before
deciding.

The revision loop in `mission.dispatch` works as follows:
- Round 0: specialist runs normally
- If `--review` and Reviewer rejects: re-run specialist with Reviewer feedback as `extra_context`
- Repeat up to `max_revisions` times
- If loop exhausts without approval: `MissionStatus.REVIEW_REJECTED`

**Model**: `DEFAULT_REVIEWER_MODEL = DEFAULT_MODEL` (imported from `specialist.py`).

---

### `runner.py` — SDK session wrapper

**`run_specialist()`** (async) — wraps `claude_agent_sdk.query()`:
- Streams every message to `events.jsonl` (flushed per line) and to `on_message` callback
- Respects `RunLimits`: `max_turns=50`, `max_budget_usd=5.0`, `max_wall_seconds=1800`
- Captures `stderr` to `stderr.log` alongside events for diagnostics
- When `can_use_tool` callback is set, switches to streaming-input protocol (required by SDK)
- SDK/subprocess errors are caught and returned as `RunStatus.ERROR` (not re-raised), so sibling parallel sub-missions survive

**`RunLimits`** — dataclass; default limits as above

**`RunStatus`** (StrEnum): `COMPLETED`, `ERROR`, `WALL_TIMEOUT`, `INTERRUPTED`

---

### `permissions.py` — Path-ownership enforcement

Implements the runtime side of the Manager's path-lane contract.

**`make_path_owner_callback()`** returns a `can_use_tool` async callback that:
- Allows all reads (Read, Glob, Grep, Bash are not gated)
- Denies `Edit`/`Write`/`MultiEdit`/`NotebookEdit` whose target path falls outside the declared `owns_paths` lane (minus `excludes_paths`)
- Resolves paths relative to the mission cwd; denies any write outside cwd entirely

Used only for parallel sub-missions where the Manager declared `owns_paths`.
Single-specialist missions have no lane and no callback.

---

### `globmatch.py` — Glob utilities

Two shared responsibilities:

**`glob_to_regex(pattern)`** — compiles a path-glob to an anchored regex.
Supports `*` (intra-component), `?`, `**` (multi-component), `[chars]`.
Hand-rolled because Python 3.13 extended `fnmatch` in incompatible ways and
Workforce supports 3.11+.

**`globs_overlap(pattern_a, pattern_b)`** — returns True if any path string
could match both patterns. Uses LRU-cached segment-level and token-level
recursive overlap checks. Used by `manager._check_parallel_overlap` to catch
decomposition conflicts before any agent runs.

---

### `utils.py` — Shared internals

```python
_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)
```
Shared by `mission.py`, `manager.py`, and `reviewer.py` for parsing fenced JSON
blocks from LLM output.

```python
def _dump_toml(data: dict) -> str:
    return tomli_w.dumps(data, multiline_strings=True)
```
Used by `specialist.py` and `project.py` to serialize TOML with multi-line
string rendering.

---

### `paths.py` — Filesystem layout

All on-disk state lives under `WORKFORCE_HOME`:
```python
home()          # $WORKFORCE_HOME or ~/.workforce
roster_dir()    # home() / "roster"
projects_dir()  # home() / "projects"
```
`ensure_layout()` creates the base directories on first use.

---

### `output.py` — Unified output

All user-visible text goes through this module (Rich-backed):
`info`, `success`, `warn`, `fail`, `rule`, `print_table`, `raw`, `die`.
`die()` prints to stderr and calls `sys.exit(1)`. No call site touches Rich
directly.

---

### `terminal.py` — Cross-platform terminal spawning

**`open_terminal_window(title, command, *, cwd)`** — pops up an OS-level
terminal window running `command`. Returns `True` if a window was spawned,
`False` if headless.

- macOS: AppleScript → Terminal.app (`osascript`). Uses ANSI-C `$'...'` quoting
  to avoid injection via the AppleScript double-quoted string embedding.
- Windows: `start "title" cmd /k <command>` via `subprocess.Popen(shell=True)`.
- Linux: tries a registry of 14 emulators in priority order. Priority:
  1. `$TERMINAL` env var (user preference)
  2. Detected parent terminal (via env vars, `$TERM_PROGRAM`, `$VTE_VERSION`,
     and `/proc` ancestor walk)
  3. Default registry order (ghostty → ptyxis → kitty → … → xterm)

---

### `doctor.py` — Pre-flight checks

`run_all()` runs 6 checks: Python ≥ 3.11, `claude_agent_sdk` importable,
`claude` binary on PATH, `git` binary on PATH, auth (`ANTHROPIC_API_KEY` or
warn), workforce home writable.

Used by `workforce doctor` and implicitly by the CLI before any dispatch.

---

### `version.py` — Version string

```python
__version__ = "0.1.0.dev0"
```

Setuptools reads this via `pyproject.toml`'s `[tool.setuptools.dynamic]
version = {attr = "workforce.version.__version__"}`.

---

## CLI Structure

Built with [Typer](https://typer.tiangolo.com/). Entry point: `workforce`
(registered in `pyproject.toml` scripts). All commands live under
`src/workforce/cli/`.

```
cli/
├── __init__.py     # app assembly; registers all commands
├── _common.py      # shared helpers (stores, renderers, mission lookup)
├── dispatch.py     # `workforce dispatch` and all its helpers
├── mission.py      # missions, replay, show, tail
├── roster.py       # hire, fire, roster, show, templates, refresh
├── project.py      # project subcommands (init, list, show, assign, …)
├── cleanup.py      # mission clean, mission prune, branches prune
├── manage.py       # interactive Manager chat session
├── merge.py        # auto-merge helpers (print plan, run merge)
└── panels.py       # side-by-side panel display for parallel output
```

### Command reference

| Command | Description |
|---|---|
| `workforce doctor` | Environment pre-flight check |
| `workforce hire <name>` | Add specialist to roster |
| `workforce fire <name>` | Remove specialist and their data |
| `workforce roster` | List all specialists |
| `workforce show <name>` | Show specialist details + memory |
| `workforce templates` | List built-in specialist templates |
| `workforce refresh [name]` | Re-apply latest common preamble |
| `workforce project init <path>` | Register a project |
| `workforce project list` | List registered projects |
| `workforce project show <proj>` | Show project details |
| `workforce project assign <proj> <spec>` | Assign specialist to project |
| `workforce project unassign <proj> <spec>` | Unassign specialist |
| `workforce project tail <proj>` | Tail all active missions for a project |
| `workforce dispatch <proj> <ticket>` | Dispatch a mission |
| `workforce missions <proj>` | List project missions (newest first) |
| `workforce replay <mission-id>` | Pretty-print events.jsonl |
| `workforce mission show <id>` | Show mission details |
| `workforce mission tail <id>` | Follow live mission output |
| `workforce mission clean <id>` | Remove mission worktree |
| `workforce mission prune <proj>` | Prune stale worktrees |
| `workforce branches prune <proj>` | Prune merged `workforce/*` branches |
| `workforce manage <proj>` | Interactive Manager chat session |

### Key dispatch flags

| Flag | Effect |
|---|---|
| `--specialist <name>` | Bypass Manager; dispatch directly |
| `--auto-staff / --no-auto-staff` | Allow Manager to hire/assign (default on) |
| `--review` | Enable post-mission Reviewer |
| `--max-revisions N` | Reviewer rejection loop cap (default 3) |
| `--auto-merge` | Merge completed branches after dispatch |
| `--merge-into <branch>` | Explicit merge target branch |
| `--branch <branch>` | Staging branch (creates if missing; implies `--auto-merge`) |
| `--yes / -y` | Skip decomposition confirmation prompt |
| `--panels` | Side-by-side panels for parallel output |
| `--window` | Background mission + spawn terminal window (requires `--specialist`) |
| `--background` | Background mission silently (requires `--specialist`) |

### `ConfirmDecision`

After Manager planning, the CLI shows a decomposition table and prompts:
```
Proceed? [y]es / [n]o / [d]iscuss with Manager
```
`d` triggers a replan loop: the Manager receives the user's feedback and the
prior decomposition, and produces a revised one. `ConfirmDecision.action` is
`Literal['proceed', 'cancel', 'discuss']`.

---

## Mission Dispatch Flows

### Flow 1: Single-specialist dispatch (`--specialist X`)

```
CLI
 └─ preflight (commits?, clean?)
 └─ mission.dispatch()
     ├─ load memories
     ├─ compose system prompt (base_prompt + cross_project_memory + project_memory)
     ├─ WorktreeManager.create() → WorktreeRef (or workspace dir)
     ├─ merge additional_merges (sequential dep case)
     ├─ runner.run_specialist() → RunResult
     │   └─ sdk.query() stream → events.jsonl + on_message callback
     ├─ [if --review] Reviewer loop (up to max_revisions)
     ├─ scan_commits()
     ├─ extract_memory_delta() (extra SDK turn)
     ├─ write transcript.md, result.md
     ├─ append memory (fcntl-locked)
     ├─ write meta.json (atomic)
     └─ update SpecialistStats
```

### Flow 2: Manager-driven dispatch (default)

```
CLI
 └─ manager.run_manager() → Decomposition
 └─ branch on kind:
     ├─ single → resolve one specialist → mission.dispatch()
     └─ parallel/sequential:
         ├─ validate_decomposition()
         ├─ resolve_task_specialists()
         ├─ [confirm loop, optional replan]
         └─ parallel.dispatch_parallel()
             ├─ _materialize_contract() → contract/contract.md
             ├─ _topological_waves()
             └─ for each wave:
                 └─ asyncio.gather(*[mission.dispatch() for task in wave])
                     (each with owns_paths lane + can_use_tool callback)
             ├─ audit_ownership() per completed sub-mission
             └─ write ParallelMissionMeta (atomic)
```

### Sequential vs. parallel within a decomposition

Both `"parallel"` and `"sequential"` decompositions use the same wave executor.
The difference is the `depends_on` graph: a fully-connected sequential chain
produces one wave per task, while a pure-parallel decomposition produces one
wave containing all tasks. Hybrid graphs produce intermediate wave counts.

Later waves receive a `start_point` set to the branch tip of their primary
dependency, and `additional_merges` for any additional dependencies.

### Workspace projects

- No `WorktreeManager.create()` call; `env.cwd = repo_path`
- `branch`, `base_sha`, and `worktree_path` are all `None` in `MissionMeta`
- `scan_commits()` is skipped
- `--review` is rejected at the CLI level (Reviewer is git-diff-only)
- `--auto-merge`, `--merge-into`, `--branch` are rejected at the CLI level

---

## Memory System

Workforce maintains two orthogonal memory scopes. Both are plain Markdown files,
append-only, with entries under `## <mission-id>` headers.

### Cross-project memory (specialist-level)
- **Location**: `~/.workforce/roster/<name>/memory.md`
- **Written by**: `RosterStore.append_memory()` with `fcntl.flock(LOCK_EX)`
  (Unix only; `_fcntl` is `None` on Windows and the lock is skipped)
- **Injected into prompt**: `<cross_project_memory>…</cross_project_memory>` tag
- **Semantics**: lessons that apply across any project (workflow patterns, tool
  quirks, debugging strategies)

### Per-project memory (project + specialist)
- **Location**: `~/.workforce/projects/<id>/memory/<specialist>.md`
- **Written by**: `mission._append_project_memory()` with `fcntl.flock(LOCK_EX)`
  (Linux/macOS only — this function does not guard the fcntl import)
- **Injected into prompt**: `<project_memory>…</project_memory>` tag
- **Semantics**: quirks of this specific repo (build steps, where tests live,
  conventions)

### Memory delta extraction

After each successful mission, `extract_memory_delta()` resumes the specialist's
session (via `ClaudeAgentOptions.resume=session_id`) and sends a structured
follow-up prompt. The response is a fenced `json` block with three fields.
Parsing uses `_FENCE_RE` (from `utils.py`). Timeout: 60 seconds. Failure is
silent (the mission still completes; `memory_delta_captured=False` in meta).

### Memory injection

`compose_system_prompt()` in `mission.py` concatenates:
1. `spec.base_prompt` (contains `common_preamble` + role section)
2. `<cross_project_memory>` block (omitted if empty)
3. `<project_memory>` block (omitted if empty)

---

## Build and Tooling

| Item | Detail |
|---|---|
| Build system | setuptools with src layout (`src/workforce/`) |
| Version | Dynamic via `[tool.setuptools.dynamic]`, sourced from `workforce.version.__version__` |
| Python requirement | ≥ 3.11 |
| Runtime deps | `claude-agent-sdk`, `typer`, `pydantic≥2.6`, `rich`, `tomli_w` |
| Dev extras | `pytest≥8`, `pytest-asyncio≥0.23`, `mypy` (strict), `ruff` |
| Tests | `tests/` — 17 files, one per module. Run with `pytest`. |
| Type checking | `mypy --strict` across `src/workforce` and `tests/` |
| Linting | `ruff` with E, F, W, I, B, UP rules; line-length 100 |
| CI | `.github/workflows/ci.yml` |

---

## Strengths

- **Clean role separation**: Manager plans (read-only), Runner executes, Reviewer
  audits. Each is a built-in fixed role; only specialists are hireable.
- **Atomic writes**: all `meta.json` writes use `write-to-tmp + os.replace`,
  preventing torn reads by concurrent watchers or replays.
- **File locking for memory**: concurrent sub-missions writing to shared memory
  files don't interleave entries.
- **Plan-time AND runtime lane enforcement**: the Manager validates
  non-overlapping path patterns at plan time (even in empty directories); the
  `can_use_tool` callback enforces lanes at write time during execution.
- **Pattern-level overlap detection**: `globmatch.globs_overlap` catches
  decomposition conflicts before any agent runs, using a recursive
  LRU-cached segment matcher.
- **Topological wave scheduling**: `_topological_waves` handles arbitrary DAGs,
  not just pure-parallel or purely-sequential chains.
- **Accumulating memory**: specialists learn across missions. Cross-project
  lessons survive project boundaries; per-project notes capture repo-specific
  quirks.
- **Two project kinds**: `repo` for engineering work (worktrees, commits,
  merges); `workspace` for recurring non-engineering tasks (file outputs, no git).
- **Cross-platform terminal spawning**: `terminal.py` supports 14 Linux
  terminal emulators, macOS Terminal.app, and Windows, with intelligent
  parent-terminal detection on Linux.
- **Detached dispatch**: `--window` and `--background` let the CLI return
  immediately while the mission runs in a child process.

---

## Known Limitations

- **`fcntl` not guarded in `mission._append_project_memory`**: `specialist.py`
  guards `import fcntl` with a try/except for Windows, but `mission.py` imports
  it unconditionally. Per-project memory appends would fail on Windows.

- **Reviewer is git-only**: `--review` is rejected for workspace projects.
  The Reviewer diffs `base_sha..HEAD`, which requires a git worktree.

- **`--window` / `--background` require `--specialist`**: there is no detached
  Manager-driven dispatch path. For multi-mission parallel runs, the user must
  stay attached or use `workforce manage` (which handles its own windowing).

- **Auto-merge aborts at first conflict**: there is no interactive conflict
  resolution or partial-merge recovery. The user must resolve conflicts manually
  and re-run.

- **Memory is append-only**: there is no pruning mechanism. Long-lived
  specialists on active projects accumulate ever-growing memory files. No
  summarization or compaction pass exists.

- **Sequential wave execution**: each wave waits for all tasks in the previous
  wave to complete before starting. A partially-failed wave skips dependent
  tasks but does not retry.

- **`discuss` replan requires an interactive terminal**: the `ConfirmDecision`
  feedback prompt (`d` choice) reads from stdin; it does not work in
  `--background` or CI contexts.

- **No persistent daemon**: missions must be explicitly dispatched. There is no
  scheduler, job queue, or webhook integration out of the box.

- **Manager response is unstructured JSON in a fenced block**: if the Manager
  model produces verbose preamble or commentary before the fenced block, the
  last fenced block wins. Edge cases can fail validation and require a replan.
