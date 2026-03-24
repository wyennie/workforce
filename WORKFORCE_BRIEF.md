# Workforce — Build Brief

> A CLI tool that gives developers a persistent roster of Claude specialists, assignable across projects, dispatchable on tickets, runnable in parallel via git worktrees. Workforce owns the orchestration end-to-end on top of the Claude Agent SDK.

This document is the brief. Read it end-to-end before writing code.

---

## The product in one paragraph

Today, Claude Code agents are ad-hoc: spawned per session, forgotten when the session ends. **Workforce** flips this. The user maintains a small **roster** of named specialists (e.g., "Aria the backend specialist", "Ben the frontend specialist") that persist across sessions, accumulate memory, and get assigned to **projects** (repos). When the user files a **ticket** ("add JWT refresh to /auth"), Workforce dispatches a **mission**: it picks the right specialist from the project's assigned roster, gives them a git worktree, runs them inside that worktree as a managed subprocess, and reports back when done. Multiple missions run in parallel on the same project, each in its own worktree.

Mental model: a staffing agency for AI engineers, not a session manager.

---

## Why this is worth building (competitive landscape, Jan 2026)

Short version of the prior research:

- **claude-squad** (smtg-ai, 6k stars, Go) — manual session manager via tmux + git worktrees. **No orchestration, no roles, no persistence.** Lets you watch many Claude Codes; doesn't drive them.
- **SwarmSDK / Claude Swarm v1** (parruda, Ruby) — v1 was multi-process Claude Code via MCP YAML topology. v2 abandoned Claude Code entirely, single-process via raw API. **Ruby-only and a different product now.**
- **affaan-m/claude-swarm** (Python, archived Feb 2026) — exactly the orchestration shape we want (planner → parallel workers → reviewer, file locking, budgets, replay). **Hackathon project, abandoned, 3 stars.** Useful prior art; their feature list is a v0.2/0.3 roadmap.
- **Anthropic native Agent Teams** (Claude Code v2.1.32+, experimental) — lead session + teammates + shared task list. **Experimental, no persistence across sessions, no project assignment, one team per session, no nesting, no background mode.** We considered building on top of this and rejected it: too many of Workforce's needs (background dispatch, parallel teams, persistent specialists) fall outside what Agent Teams does, so leaning on it would mean two orchestration systems instead of one.
- **jamsajones/claude-squad** — a Claude Code plugin with role agents inside a single session. Different category.

**The unowned gap** is exactly what the user wants:
1. **Persistent specialists** with memory that learns the user's projects over time
2. **Composable parallel missions** in worktrees on the same repo
3. **Background dispatch** — file a ticket, walk away, come back to a PR

No maintained tool combines these. That is Workforce's wedge.

---

## Architecture

### Workforce is the orchestrator

Each specialist is a managed Claude Agent SDK subprocess. Workforce owns the lifecycle, the prompts, the tool surface, the file system, the memory, and the replay log. We do not depend on Claude Code's experimental Agent Teams feature.

```
┌──────────────────────────────────────────────────────────┐
│  workforce CLI (Python, this project)                    │
│                                                          │
│  ┌────────────┐  ┌────────────┐  ┌────────────────────┐  │
│  │  Roster    │  │  Projects  │  │  Mission Runner    │  │
│  │  store     │  │  store     │  │  - worktrees       │  │
│  │            │  │            │  │  - subprocess mgmt │  │
│  └────────────┘  └────────────┘  │  - event log       │  │
│                                  │  - memory writeback│  │
│                                  └─────────┬──────────┘  │
└────────────────────────────────────────────┼─────────────┘
                                             │ spawns
                                             ▼
                                ┌──────────────────────────┐
                                │  claude-agent-sdk        │
                                │  subprocess              │
                                │  (one per specialist     │
                                │   per mission, in        │
                                │   the worktree's cwd)    │
                                └──────────────────────────┘
```

**Why this shape, in one line each:**
- *We own the orchestration* so background mode, parallel missions, and persistent memory are first-class instead of bolt-ons.
- *Each specialist is a real Claude Agent SDK process* so they keep all of Claude Code's tools (Read/Write/Edit/Bash/Glob/Grep/MCP), not the stripped-down API surface.
- *Worktrees per mission* so concurrent missions on one repo can't trample each other.
- *Memory lives on disk and is composed into prompts* rather than living in a vector DB. Simple to reason about, debuggable with `cat`.

### On-disk layout

```
~/.workforce/
├── config.toml                      # global config
├── roster/
│   ├── aria/
│   │   ├── specialist.toml          # name, role, model, base prompt, allowed tools
│   │   ├── memory.md                # cross-project lessons (append-only)
│   │   └── stats.json               # tasks completed, avg cost, success rate
│   ├── ben/
│   └── casey/
└── projects/
    └── <project-id>/                # stable hash of repo path
        ├── project.toml             # repo path, assigned specialists, default model
        ├── memory/
        │   ├── aria.md              # Aria's notes on this project
        │   └── ben.md
        └── missions/
            └── <mission-id>/
                ├── ticket.md
                ├── events.jsonl     # full replay log (one event per line)
                ├── result.md        # final summary
                ├── transcript.md    # human-readable assistant turns
                └── meta.json        # specialist used, branch, cost, duration, exit status
```

Project IDs are stable hashes (sha256 of the absolute repo path, first 12 hex chars) so re-registering a moved repo loses history; that's a feature, not a bug, for v0.1.

### Mission flow (single-specialist, v0.1)

1. User runs `workforce dispatch myproject "add a /health endpoint that returns build SHA and uptime"`.
2. Workforce loads the project, identifies assigned specialists, picks the best one for the ticket. **v0.1 picks via a small router prompt to a cheap model** (Haiku) given the specialists' role descriptions and the ticket text. If only one specialist is assigned, no router call.
3. Workforce creates a git worktree at `<repo-parent>/.workforce-worktrees/<project-id>/<mission-id>` on a new branch `workforce/<mission-id>`. Worktree is outside the repo so it doesn't pollute file watchers.
4. Workforce composes the specialist's system prompt: `base prompt + cross-project memory + project memory + ticket + success criteria`. Memory sections are clearly delimited so the specialist treats them as context, not instructions.
5. Workforce launches a `claude-agent-sdk` subprocess in the worktree's cwd, with the composed system prompt and the ticket as the first user message. Tool allowlist comes from the specialist's `specialist.toml`.
6. Workforce streams events from the SDK; every event written to `events.jsonl` immediately. Assistant turns also accumulated in `transcript.md`. stdout shows a compact human-readable feed.
7. The specialist works to completion. Completion = SDK reports a final assistant turn with no pending tool calls, OR a hard limit (max turns / wall time / cost) is hit.
8. Workforce makes one final SDK call asking the specialist for: a one-paragraph result summary, and a one-paragraph memory delta ("what did you learn about this project that the next mission should know?"). Written to `result.md` and appended to the relevant memory file.
9. Workforce prints a summary: branch, worktree path, cost, duration. **It does not auto-commit, auto-PR, or auto-clean the worktree.** The user takes it from there.

### Git commit policy

Specialists commit their own work as they go, in the worktree's branch. This is non-negotiable: a long mission with one giant final commit is hostile to review.

**Cadence rules** baked into every specialist's system prompt:
- Commit after each meaningful unit of work — a feature subtask done, a test passing, a refactor isolated. Not after every file edit; not only at mission end.
- Commit before any risky operation (large refactor, dependency change, destructive command) so the previous state is recoverable.
- Always commit before ending the mission. The branch should never have uncommitted changes when Workforce extracts the result.

**Commit message format** (enforced by prompt; we don't validate, we ask politely and trust):
- Conventional-commits style: `<type>(<scope>): <subject>` where type is one of `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
- Subject in imperative mood, under 72 chars.
- Body explains *why*, not *what*, when the change isn't self-evident from the diff.

**Authorship and trailers — important:**
- Commits are authored as the user, using the repo's existing `user.name` and `user.email`. Workforce does not override these.
- **No `Co-Authored-By: Claude <noreply@anthropic.com>` trailer.** Claude Code's default behaviour adds this; we suppress it.
- **No `🤖 Generated with Claude Code` line.** Same reason.
- The specialist's identity (which one did the work) is recorded in `meta.json` and the mission replay log, not in the commit message. Git history stays clean.

**How we suppress the Claude trailer:** the specialist's system prompt explicitly forbids it. Belt-and-braces: a post-commit check in the mission runner inspects each new commit's message and refuses to finish the mission if the trailer leaked through (with a clear error so the user can amend). Trust the prompt; verify with the check.

### Concurrency

Multiple `dispatch` calls on the same project are allowed. Each gets a separate worktree, separate mission ID, separate subprocess. They don't see each other in v0.1. Memory writes are append-only with file locks (`fcntl.flock`) so concurrent updates serialize cleanly. We accept that two missions may produce slightly redundant memory entries; reconciliation is a v0.3 concern.

### What's deferred

These are intentionally not in v0.1, with the understanding that the design above accommodates them:
- **Multi-specialist missions** — wait for v0.2 once the single-specialist loop is stable. Will require: a TaskBoard tool exposed to the lead, file lock manager, cross-specialist message routing.
- **Background daemon** — wait for v0.2. The mission runner is already structured around event streams, so daemonizing is mostly process management.
- **Memory smarts** — semantic search, summarization, decay. v0.1 just appends and loads the whole file.
- **Auto-PR / commit** — v0.2.
- **Cross-repo missions** — much later.

---

## v0.1 scope

In scope:
1. **Roster CRUD** — create, edit, list, delete specialists. Templates: `backend`, `frontend`, `tester`, `reviewer`, `generalist`.
2. **Project register and assignment** — track repos, assign roster members.
3. **Foreground single-specialist mission dispatch** — the flow above.
4. **Mission runner** — worktree management, subprocess management, event logging.
5. **Append-only memory** with composition into prompts.
6. **Replay** — pretty-print events.jsonl.
7. **Doctor** — sanity checks before anything else.

Explicitly out of scope for v0.1: parallel specialists in one mission, background daemon, auto-PR, semantic memory, TUI, notifications, integrations.

---

## CLI surface (v0.1)

```
workforce hire <name> --role <role> [--model <model>] [--from-template <tmpl>]
workforce fire <name>
workforce roster                       # list specialists
workforce show <name>                  # show one specialist incl. memory

workforce project add <path> [--name <name>]
workforce project assign <project> <specialist> [<specialist>...]
workforce project unassign <project> <specialist>
workforce project list
workforce project show <project>

workforce dispatch <project> "<ticket>" [--specialist <name>]
                                        # foreground; --specialist overrides router

workforce missions <project>           # list past missions
workforce mission <mission-id>         # show mission summary
workforce replay <mission-id>          # tail events.jsonl, formatted

workforce doctor                       # checks claude SDK, git, etc.
```

Boring, bash-friendly. No TUI in v0.1.

---

## Tech stack

- **Python 3.11+**
- **claude-agent-sdk** — hard dependency from day one, primary integration mechanism. Don't shell out to `claude` for the mission loop.
- **Click** for CLI
- **Pydantic v2** for config models and event schemas
- **tomli / tomli_w** for TOML
- **rich** sparingly, for output formatting and the replay viewer
- **anyio** if any concurrency in v0.1; otherwise stdlib threading is fine
- Shell out to `git` for worktree commands (subprocess + parsed output beats GitPython for this)

Package as `workforce` on PyPI. Single CLI entry point: `workforce`.

---

## Build order

1. **`workforce doctor` first.** Verify Python version, `claude-agent-sdk` import works, `git --version`, `ANTHROPIC_API_KEY` set. Useful for the rest of dev.
2. **Roster CRUD** — file persistence, templates, `roster/show/hire/fire`. Test: create, edit, list, delete.
3. **Project register + assignment** — same shape, separate store.
4. **Worktree manager** — separate module, well-tested. Create, list, prune, force-clean. Handle: uncommitted changes in main (refuse with clear message), worktree path collision, stale worktrees from crashed missions.
5. **Specialist runner** — given a worktree, a specialist, a composed prompt, run a `claude-agent-sdk` session to completion. Stream events to a callback. Enforce limits (max turns, wall time, cost).
6. **Mission orchestrator** — composes 4 + 5, plus router (if multiple specialists assigned), plus event log, plus result + memory writeback.
7. **Replay viewer** — pretty-print events.jsonl.
8. **End-to-end smoke test** on a real repo. Use the Definition of Done below.

Test each layer before moving on. The worktree manager especially — git is unforgiving and silent failures here cause data loss.

---

## Things to push back on

If during implementation something here is wrong, naive, or contradicts the current `claude-agent-sdk` reality, **say so in your first response with reasoning**. The brief is a starting point; you have local context (latest SDK, current flags, real subprocess behaviour) I don't. Disagreement with reasoning is welcome; silent compliance is not.

Specifically uncertain:
- Whether `claude-agent-sdk` has stable streaming event APIs that fit this shape, or whether we'd need a thin wrapper over the CLI's stream-json mode for v0.1.
- The "router prompt to a cheap model" step — it might be cleaner to just require `--specialist` in v0.1 and defer routing entirely.
- Whether `~/.workforce/` is the right home (vs `$XDG_DATA_HOME/workforce/`).
- Whether worktrees should live inside the repo (`<repo>/.workforce/worktrees/`) or alongside (`<repo-parent>/.workforce-worktrees/<project-id>/`). The brief says alongside; both have downsides.
- Whether the final "extract memory delta" SDK call is reliable, or whether we should have the specialist write memory mid-mission via a custom MemoryAppend tool.
- Whether the post-commit trailer check should *refuse* the mission (current brief) or *amend* the offending commit automatically. Refusing is safer; amending is less friction.
- Whether commit cadence guidance in the system prompt is enough, or whether we need a more structural mechanism (e.g., the runner itself triggers commits at task-list checkpoints).

Flag these and any others before committing to an approach.

---

## Style and quality bar

- Code should read like it was written by someone who has done this before, not LLM mush. Short functions, clear names, no over-abstraction.
- Type hints everywhere. `mypy --strict` should pass.
- One test file per module. Real tests, not stubs. `pytest` and `tmp_path` fixtures generously for filesystem code. Mock the SDK at the boundary; don't make real API calls in tests.
- README readable in 3 minutes. No marketing, no emoji walls.
- All output through one module so we can swap rich for plain output later.
- Subprocess management is the riskiest layer — own it carefully. Always reap children, always close streams, always respect Ctrl-C.

---

## Definition of done for v0.1

A user can:
1. `pip install -e .`
2. `workforce doctor` passes.
3. `workforce hire aria --role backend --from-template backend`
4. `workforce hire ben --role frontend --from-template frontend`
5. `workforce project add ~/code/myapp`
6. `workforce project assign myapp aria ben`
7. `workforce dispatch myapp "Add a /health endpoint returning build SHA and uptime"`
8. Watches the specialist work in real time, sees events stream, sees a worktree appear, sees a `result.md`, sees Aria's project memory file updated.
9. `workforce replay <mission-id>` prints a clean event log.
10. The mission's branch has multiple sensible commits (not one giant one), all authored as the user, none with the Claude co-author trailer.
11. `cd` into the worktree, runs the change, decides whether to merge.

If that flow works end-to-end on a real repo, v0.1 ships.
