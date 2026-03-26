# Decisions

A running log of implementation choices, with reasoning. Add a dated entry whenever you depart from the brief or pick between live alternatives. Future-you (and reviewers) read this to understand *why*.

Format: one entry per decision, newest at bottom of each section.

---

## Layer 1 — Skeleton & doctor (2026-05-02)

### Python tooling: stdlib `venv` + `pip`, not `uv`
`uv` is not installed in this environment. Stdlib works and keeps the install instructions one paragraph for new contributors. If the project picks up `uv` later, swap is a README change.

### CLI framework: Typer (over Click)
Brief listed Click. Typer is a thin Click wrapper that types cleanly under `mypy --strict` without per-decorator `# type: ignore`. We keep all of Click's behavior; we lose nothing.

### Package layout: `src/`
Forces editable installs to actually exercise the installed package, not the source tree on `sys.path`. Standard for new Python projects.

### Workforce home: `~/.workforce/`, override via `$WORKFORCE_HOME`
`~/.workforce/` matches the discoverability pattern of `~/.claude/`, `~/.aws/`, `~/.gnupg/`. XDG fans get an env override. We do not auto-fall-back to `$XDG_DATA_HOME` because dual-default behavior is a debugging hazard.

### Worktree location: `~/.workforce/projects/<id>/worktrees/<mission-id>` (departs from brief)
Brief proposed `<repo-parent>/.workforce-worktrees/`. Putting worktrees under the Workforce home instead has three wins:
- Parent-dir-not-writable is no longer a failure mode.
- No collisions across repos with the same name.
- Editor file watchers and `find`/`grep` from the user's code dir stay clean.

Cost: worktree is far from the source repo. Mitigation: dispatch summary prints the path; later add a `workforce mission cd <id>` helper or shell function.

### Doctor checks
Python ≥ 3.11, `claude-agent-sdk` importable, `claude` CLI on PATH (and runnable), `git` on PATH, auth (`ANTHROPIC_API_KEY` set OR rely on CLI session — warn, don't fail), Workforce home writable.

Auth check is intentionally a *warning* when the env var is unset: the SDK can use the `claude` CLI's existing session, and we can't introspect that cheaply. The first real SDK call will surface a clear auth error if neither path works.

### Output
All user-visible output goes through `workforce.output`. Backed by `rich` today; we can swap to plain text or capture for tests without touching call sites. Stderr for warn/fail, stdout for everything else.

### Default mission limits (planned, not yet wired)
50 turns, 30 minutes wall time, $5 cost. Override per-specialist (`specialist.toml`) and per-dispatch (`--max-turns`, `--max-cost`, `--max-wall`). Numbers are picked to be "annoying enough to bound runaway loops, generous enough to finish a real ticket."

### Project identification (planned, not yet wired)
SHA-256 of absolute repo path, first 12 hex chars. *Plus* an in-repo marker file `.workforce-project-id` written on `project add`. If the marker exists on subsequent operations, use it; else fall back to the path hash. Fixes the "I `mv`'d my repo and lost its memory" footgun without adding a registry.

### What we deferred (deliberately)
- Router-prompt-to-cheap-model: cut from v0.1. `--specialist` required when >1 assigned; auto-pick when 1.
- Auto-amending Claude trailers: refusing the mission instead. Less magic, easier to debug.
- Memory-via-tool-calls: v0.1 uses the brief's "final SDK call asks for memory delta" approach with a tight JSON-fenced schema and skip-on-parse-failure. Tool-based memory writes wait for v0.2.

---

## Layer 2 — Roster CRUD (2026-05-02)

### Names: `^[a-z][a-z0-9_-]{0,31}$`
Lowercase identifier-like names so they're shell-safe, URL-safe, and filesystem-safe everywhere. 32-char cap is arbitrary but generous; a roster of 32-char names would be unreadable anyway.

### Default model: `claude-sonnet-4-6`
The workhorse. Brief is silent on this. Per-specialist override via `--model` at hire time; per-dispatch override later.

### Common preamble baked into every template's `base_prompt` at hire time (per-name)
Commit policy, co-author-trailer rule, working-style notes. Saved into the specialist's TOML so `workforce show` reveals exactly what the model will see and the user can edit it. Per-name (function, not constant) because the trailer instruction names the specialist explicitly: `Co-Authored-By: aria <aria@workforce.local>`. Tradeoff: updating the rules globally requires re-saving every specialist (will add a `roster update-prompts` migration command if/when needed). Worth it for transparency.

**Policy clarification (2026-05-02, per user):** specialists ARE expected to commit, AND ARE expected to add a `Co-Authored-By` trailer naming themselves with a `<name>@workforce.local` email so commit attribution credits the specific worker. The forbidden thing is the *generic* `Co-Authored-By: Claude <noreply@anthropic.com>` (which would mask which specialist did the work) and the `🤖 Generated with Claude Code` line.

### Reviewer template lacks `Write` and `Edit`
Structural enforcement that a reviewer doesn't accidentally modify code. `Bash` is kept for read-only investigation (running tests, `git log`, `grep`). The prompt also says "you do NOT modify code" — belt and braces.

### Schema versioning
Every persisted Pydantic model has a `schema_version` field starting at `1`. `extra="forbid"` so unknown fields blow up loudly during loads, forcing migrations to be explicit.

### `RosterStore` re-reads from disk every call (no in-memory cache)
Roster sizes are tiny (handfuls). A cache would only buy us a way to serve stale data. Re-read is fine.

### `save(..., overwrite=True)` preserves stats and memory
Editing a specialist's role/prompt shouldn't reset their mission history or wipe what they've learned. We only initialize stats/memory if missing.

### `append_memory` uses `fcntl.flock`
Brief calls for this. Concurrent missions on different projects can both append to the same specialist's cross-project memory; without locking, writes interleave.

### `fire` requires confirmation
Default behavior prompts; `--yes`/`-y` skips. Brief is silent on this. Deletion is irreversible (memory + stats go too) so the friction is worth it. Matches `apt remove`, `gh repo delete`, etc.

### CLI commands split into `cli_*.py` modules
`cli.py` was getting long. Each command group lives in its own module (`cli_roster.py`, `cli_project.py`); `cli.py` is the registration point. Functions decorated with Typer remain importable as plain callables, which keeps testing easy.

---

## Layer 3 — Project register & assign (2026-05-02)

### Project ID scheme
12 hex chars from `sha256(absolute_repo_path)`. Plus an in-repo `.workforce-project-id` marker file written at registration. Resolution prefers the marker, falls back to path hash. Fixes the "I `mv`'d my repo and lost its memory" footgun without a separate registry. Marker is committed by default — encourages teams to share project identity.

### Marker write is best-effort, not fatal
If the repo is read-only at `project add` time, we warn and continue. The user keeps a working registration; they just lose the move-survives-rename property. Failing the registration over a marker write would be over-strict.

### Display names must be unique (case-insensitive)
Brief doesn't say. Names are how humans refer to projects (`workforce dispatch myapp ...`); collisions would force ID-based reference. We reject the second registration of the same name and tell the user to disambiguate with `--name`.

### `resolve(ref)` accepts full ID or display name
ID is the on-disk dir name; users will only ever see it printed. Display name is canonical for everything human-facing. We don't accept ID *prefixes* in v0.1 — too easy to ship a partial-ID bug; full-12-hex match only.

### `project forget` (added; not in brief)
Brief lists `add/assign/unassign/list/show` but no removal. Without one, users have to `rm -rf ~/.workforce/projects/<id>/`. `forget` removes the registration AND its memory and mission history but does NOT touch the user's repo or its marker file. Requires `-y` confirmation. Documented as a v0.1 deviation.

### `default_model` field on Project
Reserved for "this project should run all specialists with X model" override. Not yet wired into dispatch — planned for layer 7.

### TOML serialization: `model_dump(exclude_none=True)`
TOML has no null type. Trying to serialize a `None` field crashes `tomli_w`. Pydantic's `exclude_none` drops the key on dump; we lose nothing because Pydantic re-defaults on load.

---

## Layer 4 — SDK smoke test (2026-05-02)

`scripts/sdk_smoke.py` is a throwaway one-shot session that ran end-to-end and confirmed the SDK's actual shape. Key findings, all of which shape the runner design:

### Streaming surface
`claude_agent_sdk.query(prompt=..., options=ClaudeAgentOptions(...))` returns an `AsyncIterator` of message types: `SystemMessage` (init/etc), `AssistantMessage`, `UserMessage` (carries tool results), `ResultMessage` (final), `RateLimitEvent`. All dataclasses → `dataclasses.asdict()` for JSONL logging.

`ResultMessage` carries everything we need for `meta.json`: `subtype`, `duration_ms`, `num_turns`, `is_error`, `total_cost_usd`, `errors`. Brief was accurate; runner writes meta directly from this.

### `ClaudeAgentOptions` knobs we'll use
- `cwd`: worktree path (verified — model self-corrected on first wrong write)
- `system_prompt`: composed prompt (preamble + base + memory + ticket)
- `allowed_tools`: filter on what the model may invoke
- `model`: per-specialist Claude id (must be set explicitly — see below)
- `max_turns`: hard cap (50 default)
- `max_budget_usd`: native cost cap ($5 default)
- `permission_mode='bypassPermissions'`: unattended dispatch
- `extra_args`: dict[str,str|None] — escape hatch if the SDK lacks a knob we need

### The SDK session inherits the user's Claude Code environment
The `system:init` event reports 33 tools available, 3 MCP servers, 4 agents, 8 skills, 19 slash commands, plus the user's memory paths. `allowed_tools` only restricts what the model is *permitted to invoke* — everything else stays registered.

This is mostly a feature (specialists inherit the user's MCP setup) but it's an unstated dependency: a mission's behavior depends on the user's local `claude` config. v0.1 accepts this; v0.2 should consider `setting_sources=[]` to fully isolate.

### Auth via `claude` CLI session, not env
`apiKeySource: 'none'` confirms the SDK uses the `claude` CLI's logged-in session when no env key is present. Doctor's "auth WARN when env unset" decision was correct.

### The default model is the user's `claude` CLI default
With `model=None`, the smoke ran on `claude-opus-4-7[1m]` (the user's default). The runner MUST pass `model=spec.model` explicitly so each specialist gets the model their TOML says, not whatever the user happened to set globally.

### Cost data point
A 5-turn smoke that wrote one file cost $0.1473 on Opus 4.7. The default `max_budget_usd=5.00` is comfortably above any small ticket and well below runaway territory.

---

## Layer 5 — Worktree manager (2026-05-02)

### Shell out to `git`, no GitPython
Six git operations (`worktree add`, `worktree remove`, `worktree list --porcelain`, `worktree prune`, `status --porcelain`, `show-ref`). Subprocess + a 30-line porcelain parser is less code than wiring GitPython, and one fewer runtime dep.

### Dirty-repo policy: refuse on staged/modified, tolerate untracked
Brief said "refuse on uncommitted changes". Strict reading would also refuse untracked files, which is hostile — most repos accumulate scratch files (notes, .env.local, test outputs). We refuse only on `git status --porcelain` lines that don't start with `??`. The error message lists the first three offending paths so the user knows what to commit/stash.

### Branch naming: `workforce/<mission-id>`
Distinctive prefix so users can `git branch -l 'workforce/*'` to list everything Workforce has touched. Mission IDs are restricted (`^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$`) so they're branch-name-safe and dir-name-safe in one go.

### `remove(force=True)` falls through to `shutil.rmtree`
When `--force` is passed but git's removal still leaves the dir behind (rare but observed in pathological states), we follow up with `rmtree`. Without force, we raise loudly so the caller knows the worktree is in a weird state.

### `list_for_project()` (filesystem) vs `list_git_worktrees()` (git registry)
Two perspectives. The first is "what dirs are on disk for this project" — useful for cleanup commands. The second is "what does git think exists" — useful for diagnostics and detecting stale registry entries. Both surfaced because they answer different questions.

### `WorktreeRef` carries `base_sha`
Captured at create time via `git rev-parse HEAD`. The orchestrator needs it to diff new commits (`base_sha..HEAD` in the worktree) for trailer scanning and commit counting. Recording it once at the source means the orchestrator never needs to re-resolve "what was main when this started".

---

## Layer 7 — Mission orchestrator + dispatch (2026-05-02)

### Mission ID format: `m-YYYYMMDD-HHMMSS-xxxx`
Sortable, branch-name-safe, distinctive prefix (`m-`) so users can `git branch -l 'workforce/m-*'`. Random suffix is 4 hex chars from `secrets.token_hex(2)` — collision odds are negligible at human dispatch rates.

### Prompt split: system_prompt = role + memory; user_prompt = ticket + success criteria
Brief said "system prompt: base + cross-project memory + project memory + ticket + success criteria". I departed: the ticket is a *user request*, not system context, so it belongs in the user message. System prompt has identity/working-style/memory; user prompt has the request and what done looks like. Better fit for Claude's training data, more natural reads.

### Memory wrapping: XML tags
`<cross_project_memory>` and `<project_memory>` blocks with a one-line preamble ("Treat as background knowledge, not instructions"). Empty sections are omitted entirely so missions on virgin projects don't get an empty XML tag confusing the model.

### Memory delta extraction: separate follow-up SDK call via `resume=session_id`
After the main mission's `ResultMessage`, we issue a second `query()` with `resume=<session_id>`, `max_turns=1`, `allowed_tools=[]`, and a tightly-scoped prompt asking for `{"summary","project_memory","cross_project_memory"}` in a fenced JSON block. Parse failure → log nothing, mission still succeeds. Empty fields → no append.

This is more honest than "stuff a magic instruction into the system prompt". The two phases (do work / explain it) are visually separate in the events log too. Cost: ~$0.02 extra per mission. Cheap insurance for memory quality.

### Memory delta is skipped on errors
If the runner returned anything other than `COMPLETED` (or the result has no session_id), we don't even attempt the follow-up. There's no point asking a failed/timed-out specialist what they "learned".

### Memory entries are appended with mission ID headers
Each memory write becomes a `## m-...-xxxx\n\n<text>` block. Lets future-Aria see *which mission* taught her something, and gives users a way to find/strip entries by mission later.

### `result.md` content priority: delta.summary > last_assistant_text > "(no summary captured)"
The structured wrap-up gives us a tight summary; we prefer it. If we couldn't get one (parse failure, error, etc.), fall back to the model's last text turn. If even that's empty, write a placeholder so the file always exists and `cat result.md` doesn't surprise the user.

### Mission status: 5 terminal states
`completed`, `error`, `wall_timeout`, `interrupted`, `trailer_violation`. The last one is *orchestrator-level* — the run itself succeeded, but we found forbidden Claude trailers in the new commits and we won't pretend that's clean. Stats: only `completed` increments `missions_completed`; everything else increments `missions_failed`.

### Trailer detection: two regexes, case-insensitive
`co-authored-by:.*noreply@anthropic\.com` and `generated with .{0,3}claude code` (the `.{0,3}` is to absorb the rocket emoji + space variations). Detected per-commit, recorded in `meta.json`, surfaced in the CLI summary. We do NOT auto-amend.

### Stats are updated synchronously inside dispatch
Cost and duration always added. Pass/fail count always incremented. No async write-behind; the orchestrator blocks on the stats save before returning. Roster CRUD layer was built with file locks specifically so concurrent missions can do this safely.

### `dispatch()` takes stores, not paths
The orchestrator is a single async function that mutates the stores. Keeps the call site (CLI) simple — just construct stores and call dispatch. Tests use real stores in `tmp_path` and mock the runner + memory call. No mocking of file IO.

### CLI live renderer: text + tool-use, hide thinking + init noise
`AssistantMessage` text blocks render verbatim. `ToolUseBlock` renders as `→ name(arg=value)` with one chosen interesting argument. `ToolResultBlock` only renders if `is_error`. `ThinkingBlock` and `SystemMessage(init)` are skipped — too noisy for the live view, still in `events.jsonl` for replay.

### CLI warns on suspiciously few commits
If a mission completes with `len(commits) < 2`, we print a warning. Brief calls for "warn (don't fail) if a mission completes with only one commit and >N tool uses". I simplified to just `< 2`; tool-use count is in events.jsonl if anyone wants to refine the heuristic.

---

## Layer 8 — Replay & cleanup (2026-05-02)

### CLI surface deviations from brief
Brief listed `workforce mission <id>` (top-level, takes id arg) AND `workforce missions prune` (sub-command of `missions`). That has Typer-friction: `mission` can't both take an arg and host sub-commands cleanly.

Final shape:
- `workforce missions <project>` — top-level (brief-compliant).
- `workforce mission show <id>` / `mission clean <id>` / `mission prune` — sub-typer.
- `workforce replay <id>` — top-level (brief-compliant).

So `mission show <id>` replaces brief's `mission <id>`, and `mission prune` replaces brief's `missions prune`. Adds `show` as an explicit verb; same intent.

### Mission lookup is project-scan, not indexed
`_find_mission(id)` iterates registered projects and reads each `meta.json`. With at most a few-dozen projects × a few-hundred missions each, this stays well under 100ms. An index file would be one more thing to keep consistent for negligible payoff.

### Replay reads JSONL and renders dicts directly (doesn't reconstruct SDK types)
The live renderer works on `claude_agent_sdk` dataclasses; the replay renderer works on plain dicts. Two renderers, one shape. Reconstructing typed objects from JSONL would be brittle (SDK schema changes leak in) and add no value — replay only needs to show what happened.

### `mission clean` keeps the branch and the mission directory
Only the worktree dir + git's worktree registry entry are removed. The mission's branch lives in the source repo where the user can `git merge` or `git branch -D` it. The mission's logs and meta stay so `replay` and `mission show` keep working forever.

### `mission prune --keep-failed`
Default is to prune failed missions too — the worktree of a failed mission is usually trash. Flag is for the case where the user wants to keep failed worktrees around for debugging. Inverse of typical "keep good, drop bad" but matches Workforce semantics: failed worktrees are the same on-disk artifact as completed ones, the user just hasn't cleaned them up yet.

### Duration parser: small, hand-rolled, units `h/d/w/m`
`_DURATION_RE` covers what humans actually type for cleanup intervals. `m` is 30 days (calendar month is approximate; close enough for pruning). No `y` because if you have year-old worktrees you have bigger problems.

---

## Layer 9 — End-to-end smoke on real repo (2026-05-02)

Walked the brief's Definition of Done on a fresh scratch repo. All criteria met. Real-call cost: $0.21 for the mission + ~$0.02 for the memory delta call.

### What worked first try
- doctor → hire → project add → assign → dispatch flow works as designed.
- Live event streaming (text + tool calls + errors) renders cleanly.
- Worktree appears at `~/.workforce/projects/<id>/worktrees/<id>/`, isolated from source.
- All artifacts written: `events.jsonl`, `meta.json`, `result.md`, `ticket.md`, `transcript.md`.
- Memory delta call returned a valid JSON block; project memory and cross-project memory both updated with **substantive** content (Aria noticed PEP 668 + src-layout quirks).
- Stats updated correctly.
- Branch on the source repo authored as the user; commit message in conventional-commits style; **no Claude trailer**.
- `replay`, `mission show`, `missions list` all render correctly on the resulting mission.
- Code Aria wrote (`greet()` function) executes correctly out of the worktree.

### What surfaced
1. **Aria committed once at the end, not as she went.** Despite explicit cadence rules in the prompt, the `< 2 commits` warning fired correctly. Confirmed v0.1 limitation: prompt-only cadence is unreliable. Fix is structural (TaskBoard tool / runner-driven checkpoints) — v0.2.
2. **Initial tool calls used `/root/repo/...` (a stale Claude default).** Same pattern as the SDK smoke. The model self-corrects via `pwd` after the first error. Could be improved by adding the cwd more prominently in the user prompt — e.g. open with "You are working in `<cwd>`." Worth doing in v0.2; for now it costs ~3 wasted turns and the model recovers.
3. **`mission clean` error message leaked the Python kwarg name** (`Try with force=True`). Fixed: now says `pass force=True (CLI: --force)`. Real bug caught by the e2e — exactly what the e2e is for.

### Cost data point (real)
22-turn ticket (read 4 files, write 3, edit 1, run 5 bash commands, commit) on Sonnet 4.6: $0.21. Memory delta follow-up call: $0.02. Total $0.23. Default `max_budget_usd=5.00` has comfortable headroom for tickets ~10x this size.

---

## v0.2 — Parallel dispatch with Manager (2026-05-02)

The wedge product feature: one ticket fanned out across multiple specialists working concurrently in their own worktrees, with a Manager doing contract-first decomposition. Reviewer deferred to v0.3 — parallelism is the value, the Reviewer is the quality gate.

### Manager is a built-in role, not a hireable specialist
Hiring a Manager would be an extra setup step for the user and add a "you forgot to hire one" footgun. The Manager is invoked implicitly when `--parallel` is used. Default model `claude-sonnet-4-6` (planning needs the smarter model; Haiku would skimp on contract quality). Allowed tools: `Read, Glob, Grep` only — read-only by construction. No memory or stats yet (would be useful future addition: "manager learned that this project's auth module is hard to parallelize").

### Decomposition kind: parallel | sequential | single
The Manager's first decision is which kind. `single` is the **graceful fallback**: when the ticket genuinely doesn't slice well, force-decomposing produces merge headaches worse than just running one specialist. The Manager's prompt rewards picking `single` honestly over bluffing `parallel`.

### Contract-first as the parallelism enabler
For a ticket like "refactor the auth module", the Manager produces a contract (typed signatures, behavior, edge cases) FIRST, then fans out impl/tests/callers/docs against that contract. Each specialist works against the same fixed API, in disjoint files. Without a crisp contract, parallelism is just N specialists guessing at the same API in their own way — and merging will be a nightmare.

The contract is materialized once at `<parent-mission>/contract/contract.md` and injected into each sub-mission's user prompt as an `<extra_context>` block. Inline beats file-based for first cut: the contract is short, model treats inline content as authoritative, no re-read latency.

### Filesystem-grounded path overlap detection
For each pair of parallel tasks (no transitive dependency between them), resolve `owns_paths - excludes_paths` against the actual repo. If their concrete file sets intersect, validator refuses the decomposition. **Documented limitation**: tasks that create entirely-new files won't overlap (the files don't exist yet at validation time). Common case (refactoring existing files) IS caught. Symbolic glob-overlap analysis is hard and not worth it for v0.2.

### DAG check via Kahn's algorithm
`depends_on` builds a graph; topological sort raises `ValidationError` on cycle. Plus a separate check that `merge_order` covers every task and respects the dependency order. Two checks because they catch different bugs.

### Sub-mission ID format: `<parent-id>__<task-id>`
Flat alongside the parent in `missions/`, NOT nested. Reuses `_find_mission` and `mission show` / `replay` machinery as-is for sub-missions. Sorts naturally. Slight loss of visual hierarchy in `missions <project>` output; acceptable for v0.2.

### Per-task callback factory for live UI
The CLI registers a `make_sub_callback: (task_id) -> EventCallback` factory rather than a single shared callback. Each sub-mission gets its own renderer that prefixes lines with `[task_id]`. Three streams interleave on stdout but the prefix lets the eye group them. Future: rich `Live` panels per task — meaningful for >3 parallel specialists.

### `mission.dispatch` gained `extra_context: str | None`
Single new param threaded through to `compose_user_prompt` as an `<extra_context>` XML block between the ticket and success criteria. Used by the parallel orchestrator to inject the contract; could also be used by future features (e.g., shared "what we learned" notes from earlier sub-missions).

### Specialist resolution: suggested → fallback → error
Manager's `suggested_specialist` wins if assigned to the project. If the suggestion is unassigned (or absent), fall back to a CLI-provided fallback. If only one specialist is assigned, use them. Otherwise refuse with a clear error. No router; explicit choice.

### Confirmation UX
By default print decomposition table + ask y/N. `--yes` skips. No `--edit` flag yet (would open `$EDITOR` on `decomposition.json` pre-confirm); add when the prompt isn't crisp enough that users want to tweak.

### Failure handling: each sub-mission is independent
If one sub-mission errors/times-out, the others still complete. Parent meta status: `completed` (all green) | `partial` (mix) | `failed` (all red) | `cancelled` (user said no). Merge plan shows successful branches with `git merge --no-ff` lines and lists the failed ones separately. No auto-rollback; user decides what to keep.

### v0.2 e2e cost data
"Add string_utils with kebab/snake-case + tests + README mention", parallel into 3 sub-missions:
- Manager: $0.097 (decomposed cleanly with a good contract)
- impl (aria, 8 turns): $0.107
- tests (ben, 11 turns): $0.148
- docs (casey, 7 turns): $0.091
- **Total: $0.44**, with 3 disjoint commits, **zero merge conflicts** when running the printed merge plan.

Cheaper than I expected. The contract pays for itself: each specialist worked against a fixed target so they didn't waste turns on exploration.

### v0.2 e2e findings
1. **Manager assigned wrong specialist to wrong template** (ben/frontend got `tests`, casey/tester got `docs`). Cosmetic — work was correct. Manager prompt could read template descriptions from the roster to make smarter suggestions. Easy v0.3 follow-up.
2. **Same `/root/repo/...` initial misfires** as v0.1. Each parallel specialist hit it once, wasting 1-2 turns. Worth fixing globally (prepend cwd to user prompt).
3. **Single-commit-per-task** in all three subs. For tightly-scoped subtasks this is fine — the warning we have for `<2 commits` is more relevant for the parent ticket than for each subtask. Could suppress the warning in parallel mode.
4. **Live output got busy** with 3 streams. Readable but noisy. Worth doing rich `Live` panels per task in a future iteration.

---

## v0.2.1 — Auto-staffing (2026-05-02)

### Problem
v0.2 required the user to pre-hire and pre-assign specialists for every project. For new projects or surprising tickets ("we need a docs writer here"), that was friction. The Manager already knows what specialties a ticket needs — let it staff up automatically.

### Three-tier resolution in `parallel.resolve_task_specialists`
1. **Already assigned to project** → use directly. (Default path; preserves the persistent-memory wedge.)
2. **In global roster but not assigned** → auto-assign to the project. Specialist keeps their identity and any cross-project memory; gains project memory from this mission forward.
3. **Doesn't exist anywhere** → if `template_hint` is provided, hire from template + assign + use. Specialist becomes a permanent roster member the user can `workforce show` and `workforce fire` later.

Project file is mutated and persisted exactly once at the end of resolution (one `project_store.save(overwrite=True)`), not per task — avoids racy partial writes if a later task fails.

### `Task.template_hint: str | None`
New optional field on the Decomposition's task schema. The Manager fills it whenever it suggests a name that isn't already in the project's specialist list, picking from `backend | frontend | tester | reviewer | generalist`. If the Manager omits it for a non-existent name, resolution falls back to the explicit fallback specialist or errors loudly.

### Manager sees mission counts now
Old user prompt: "Available specialists: aria, ben, casey." New: each is annotated with `(N missions on this project)` and their role description. Gives the Manager signal on **who actually knows the codebase** — a specialist with 5 missions on this repo is preferable to a fresh hire even when both could do the work. (Brief's wedge: persistent memory compounds.)

### `--auto-staff/--no-auto-staff` CLI flag, default ON
Default-on because the user explicitly asked for "look at roster, hire as needed." `--no-auto-staff` reverts to v0.2 strict behavior: refuse if Manager picks an unassigned/non-existent specialist. Single-specialist `dispatch` (non-parallel) doesn't get the flag — that's the "I know exactly who I want" path.

### Staffing actions surfaced in confirm UI
Each task row in the decomposition table shows one of: `assigned` (dim), `auto-assigned` (cyan), `auto-hired (← template)` (magenta), `fallback` (yellow). Plus a callout above the prompt listing new hires and auto-assignments by name. User sees what's about to change before saying yes.

### What we did NOT do
- **Make specialists ephemeral.** They remain persistent roster members. An auto-hired `docs-writer` lives forever (until `workforce fire`), accumulates memory, and gets reused on future missions. This preserves the "memory compounds" property; the alternative (mission-scoped throwaways) would be a different product.
- **Auto-hire on single-dispatch.** Only `--parallel` mode auto-staffs. Single dispatch still requires `--specialist` if the project has multiple assigned, or auto-picks if exactly one. Keeps the simple path simple.
- **Open `$EDITOR` on the decomposition.** Considered for letting users override Manager's specialist picks pre-confirm. Defer until users actually want it.

---

## v0.2.2 — Manager always-on as default (2026-05-02)

### `--parallel` flag dropped; Manager runs by default
`workforce dispatch myrepo "ticket"` always runs the Manager first. The Manager picks `kind: parallel | sequential | single` based on the ticket and the repo. We removed the `--parallel` flag entirely — it was a redundant ceremony since the Manager itself can return `kind=single` when nothing decomposes.

### `--specialist X` becomes the explicit bypass
For tiny tickets ("fix typo on line 23"), the Manager pass is pure overhead (~$0.05-0.10, ~10-30s). `--specialist X` skips the Manager and dispatches X directly. Documented in the help text as "use for tiny tickets where you don't need planning overhead". Requires X to already be assigned to the project — bypassing the Manager also bypasses auto-staff.

### Routing inside the dispatch handler
1. `--specialist X` set → `_dispatch_direct` (no Manager, single mission via `mission.dispatch`).
2. Otherwise → `_dispatch_with_manager` runs the Manager once, then:
   - `kind=single` → `mission.dispatch` directly with the Manager's specialist suggestion. Mission ID is flat (no `__solo` suffix). Decomposition.json saved alongside the mission for traceability.
   - `kind=parallel | sequential` → `parallel.dispatch_parallel(decomposition_override=...)` reuses the existing orchestrator without re-running the Manager.

### `kind=single` skips the confirmation prompt
There's nothing to review for a one-task plan. The Manager's verdict line ("manager: kind=single, cost=$0.05, rationale=...") is shown and the mission just runs. Confirmation only appears for parallel/sequential where there's a fan-out to confirm.

### `MissionMeta.manager_cost_usd: float = 0.0`
Tracks the planning cost per mission. Lumped into `cost_usd` for the headline number; broken out in the JSON for transparency. For parallel missions, the field lives on `ParallelMissionMeta` and represents the planning cost shared across all sub-missions. For `kind=single` routed through `mission.dispatch`, the field is on the single mission's meta directly.

### "Cost" in CLI output is a budget signal, not money
The SDK reports tokens × API price as the `cost_usd` field. Since we authenticate via the user's `claude` CLI session (which uses their Claude Code subscription quota), this number doesn't directly correspond to dollars billed unless they exceed their plan. We keep the field name as `cost_usd` because it's accurate (it IS the API-equivalent cost) and it's what `--max-cost` caps.

---

## v0.2.3 — `--auto-merge` (2026-05-02)

### Opt-in by default, not opt-out
`workforce dispatch ... --auto-merge` runs the merge plan against the source repo after a successful mission. Default is OFF. First-time users want to look at the diff before AI work lands; experienced users flip the default in a shell alias. Same logic as why we don't auto-commit inside missions: trust before automation.

### Works for all three dispatch paths
- **Direct (`--specialist X`):** one-step plan with the single mission's branch.
- **Manager → single:** same one-step plan; treats Manager's planning pass as having "blessed" the work.
- **Manager → parallel/sequential:** the existing `merge_plan()` output, executed in order.

### Behavior on conflict
First non-zero exit from `git merge --no-ff` triggers `git merge --abort` (best-effort, so the source repo isn't left mid-merge) and marks all subsequent steps as `skipped (earlier step failed)`. CLI prints which branch hit the conflict and tells the user to resolve manually. We do NOT attempt to auto-resolve — two LLMs doing 3-way text merges is exactly where small mistakes silently break things.

### Behavior on partial mission success
If the parent's status is anything other than `completed` (or any sub-mission's status is not `completed`), auto-merge is **skipped entirely**. We don't half-merge a half-failed dispatch. The merge plan is still printed so the user can pick what to keep manually.

### Auto-merge runs in the source repo's current branch
Whatever branch the source repo is checked out on at merge time is what we merge into. We don't switch branches, don't validate that the current branch is the same as the dispatch-time branch. If the user moved to a different branch between dispatch and completion, `git merge` produces a weird result — that's on them. (Could detect this in v0.3 by comparing `base_sha` to current HEAD's reachable commits.)

### `AutoMergeStepResult` records per-step outcomes
Returned from `parallel.auto_merge`, printed by the CLI, not yet persisted to `meta.json`. Persist if/when we want post-hoc reporting on auto-merge history. For now the user sees results live and the commit history is the source of truth.
