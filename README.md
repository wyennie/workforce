# Workforce

A CLI that maintains a persistent roster of Claude specialists, dispatches them on tickets, and runs them in isolated git worktrees.

Workforce is a staffing agency for AI engineers. Hire named specialists (`aria` the backend engineer, `ben` the frontend specialist, …), assign them to projects, and file tickets — Workforce picks the right specialist, isolates the work in a git worktree on a dedicated branch, streams events live, and writes back what was learned so the next mission starts smarter.

[![PyPI](https://img.shields.io/pypi/v/workforce-ai)](https://pypi.org/project/workforce-ai/) [![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://python.org) [![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![CI](https://github.com/wyennie/workforce/actions/workflows/ci.yml/badge.svg)](https://github.com/wyennie/workforce/actions/workflows/ci.yml)

---

## Table of Contents

- [Install](#install)
- [Quickstart](#quickstart)
- [Concepts](#concepts)
- [State layout](#state-layout)
- [Commands reference](#commands-reference)
  - [Setup](#setup)
  - [Roster](#roster)
  - [Projects](#projects)
  - [Missions](#missions)
  - [Branches](#branches)
  - [Memory](#memory)
  - [Ticket templates](#ticket-templates)
  - [Config](#config)
  - [Specialist marketplace](#specialist-marketplace)
  - [Webhook daemon](#webhook-daemon)
  - [Web dashboard](#web-dashboard)
  - [MCP server](#mcp-server)
- [Dispatch deep dive](#dispatch-deep-dive)
  - [How the Manager plans](#how-the-manager-plans)
  - [Decomposition confirmation](#decomposition-confirmation)
  - [Auto-staffing](#auto-staffing)
  - [Reviewer loop](#reviewer-loop)
  - [Staging branch](#staging-branch)
  - [Detached dispatch](#detached-dispatch)
- [Project kinds](#project-kinds)
- [WORKFORCE.md](#workforcemd)
- [Configuration reference](#configuration-reference)
- [Memory system](#memory-system)
- [Specialist templates](#specialist-templates)
- [Mission artifacts](#mission-artifacts)
- [Commit policy](#commit-policy)
- [Webhook daemon (advanced)](#webhook-daemon-advanced)
- [GitHub Actions](#github-actions)
- [MCP server (advanced)](#mcp-server-advanced)
- [Web dashboard (advanced)](#web-dashboard-advanced)
- [Developing Workforce](#developing-workforce)
- [License](#license)

---

## Install

#### Recommended

Install from PyPI as an isolated tool (no virtualenv management needed):

```bash
uv tool install workforce-ai
# or: pipx install workforce-ai
```

#### Version pinning

For reproducible environments, pin to a specific release:

```bash
uv tool install 'workforce-ai==0.1.0'
```

#### Install script

Installs [`uv`](https://docs.astral.sh/uv/) if missing, then installs Workforce from the GitHub repo:

```bash
curl -LsSf https://raw.githubusercontent.com/wyennie/workforce/main/install.sh | bash

# Pin to a specific release tag:
curl -LsSf https://raw.githubusercontent.com/wyennie/workforce/main/install.sh | bash -s -- --tag v0.1.0

# List available release tags:
curl -LsSf https://raw.githubusercontent.com/wyennie/workforce/main/install.sh | bash -s -- --list-tags
```

> [!WARNING]
> The `claude` CLI must be installed separately and authenticated before any dispatch will work. See [Anthropic's installation guide](https://docs.anthropic.com/en/docs/claude-code) for setup instructions.

#### From git

```bash
uv tool install git+https://github.com/wyennie/workforce
# or: pip install git+https://github.com/wyennie/workforce
```

#### Verification

Always finish with:

```bash
workforce doctor
```

`doctor` verifies Python ≥ 3.11, `claude-agent-sdk`, the `claude` CLI, git, authentication, and that your Workforce home directory is writable.

---

## Quickstart

```bash
# Hire two specialists from built-in templates
workforce hire aria --from-template backend
workforce hire ben  --from-template frontend

# Register a project and assign both specialists
workforce project add ~/code/myapp
workforce project assign myapp aria ben

# File a ticket — the Manager decides how to split the work
workforce dispatch myapp "Add a /health endpoint returning build SHA and uptime"

# Or open an interactive Manager chat and iterate:
workforce manage myapp
```

Each mission runs in its own worktree under `~/.workforce/projects/<id>/worktrees/<mission-id>/` on a `workforce/<mission-id>` branch. Workforce streams events live, writes a `result.md` summary, and prints the merge plan when the mission completes. Nothing lands on `main` until you say so — pass `--auto-merge` when you're ready to skip that manual step, or `--branch dev` to work against a staging branch.

---

## Concepts

### Specialists vs Manager vs Reviewer

- **Specialists** are the hireable named agents you manage. Each has a role, a model, a tool set, and growing memory. All the `hire`/`fire`/`roster` commands operate on specialists.
- **Manager** is a built-in read-only planning agent. It runs first on every `dispatch` call (unless `--specialist` bypasses it) and decides how to decompose the ticket into a `single`, `parallel`, or `sequential` plan.
- **Reviewer** is a built-in read-only + Bash auditor. It runs after a specialist when `--review` is set, diffs the changes, and can reject them to trigger a revision loop.

> [!NOTE]
> The Manager and Reviewer are built-in roles — you cannot `workforce hire` them. Attempts to hire a specialist named `manager` or `reviewer` are blocked.

### Missions

One mission = one ticket, one or more specialists, one or more worktrees. Mission IDs are formatted as `m-YYYYMMDD-HHMMSS-xxxx` (timestamp + 4-character random hex suffix), making them sortable and branch-safe. A parallel dispatch creates a *parent* mission and N child sub-missions, each with its own ID and branch.

### Repo projects vs workspace projects

- **Repo project**: git worktree per mission, `workforce/<mission-id>` branch, conventional-commits as the work proceeds.
- **Workspace project**: missions run directly in the project directory — no git operations, no worktrees.

> [!WARNING]
> `--review`, `--auto-merge`, and `--branch` are **not available** on workspace projects. These features require git, and callers who pass them get a CLI error.

Full comparison in the [Project kinds](#project-kinds) section.

### Memory

Specialists accumulate two types of memory:

- **Cross-project memory** — lessons that apply anywhere (debugging strategies, tool quirks, patterns).
- **Per-project memory** — this repo's conventions: where tests live, build commands, auth approach.

Both are plain Markdown files, append-only, injected into the specialist's system prompt on every mission. Full detail in the [Memory system](#memory-system) section.

---

## State layout

All on-disk state lives under `~/.workforce/` by default. Override with the `WORKFORCE_HOME` environment variable.

```
~/.workforce/
├── config.toml                         # global config (optional)
├── webhook.toml                        # webhook daemon config (optional)
├── webhook.pid                         # webhook daemon PID (runtime)
├── roster/
│   └── <specialist-name>/
│       ├── specialist.toml             # role, model, tools, base prompt
│       ├── memory.md                   # cross-project memory (append-only)
│       └── stats.json                  # lifetime cost and mission counts
└── projects/
    └── <12-hex-project-id>/
        ├── project.toml                # project registration record
        ├── memory/
        │   └── <specialist>.md         # per-specialist, per-project memory
        ├── worktrees/
        │   └── <mission-id>/           # git worktree (repo projects only)
        └── missions/
            └── <mission-id>/
                ├── ticket.md           # verbatim ticket text
                ├── events.jsonl        # full JSONL replay log (flushed live)
                ├── transcript.md       # human-readable assistant turns only
                ├── result.md           # final summary
                ├── meta.json           # structured mission record
                ├── decomposition.json  # Manager's plan (if used)
                ├── startup.log         # subprocess stderr (background runs)
                └── stderr.log          # claude CLI diagnostic stderr
```

Workforce also writes a marker file in each registered repo root:

```
<repo-root>/.workforce-project-id    # 12-hex project ID; commit this file
```

This lets Workforce resolve the project even if the repo is moved to a different absolute path.

---

## Commands reference

### Setup

#### `workforce doctor`

```bash
workforce doctor
```

Verify the environment is ready. Checks Python ≥ 3.11, `claude-agent-sdk`, the `claude` CLI, git, authentication, and that `WORKFORCE_HOME` is writable. Prints a table with `ok` / `warn` / `fail` per check. Exits with code 1 if any check fails. Run this first — before anything else, after install, and when debugging mysterious failures.

---

#### `workforce init`

```bash
workforce init [OPTIONS]
```

Scaffold a new Workforce project in the current directory. Registers the directory, optionally hires specialists from a stack template, writes `WORKFORCE.md` and `.workforce.toml`.

| Flag | Default | Description |
|---|---|---|
| `--template NAME`, `-t NAME` | — | Stack template to apply (see table below) |
| `--blank` | off | Register with no specialists |
| `--list` | off | Print available stack templates and exit |
| `--name NAME` | directory basename | Project display name |
| `--demo` | off | Create a toy calculator demo project in a temp dir |

`--template` and `--blank` are mutually exclusive.

> [!TIP]
> `workforce init --demo` spins up a toy calculator project — good for exploring Workforce with zero setup.

**Stack templates** (pass to `--template`):

| Template | Specialists hired | Reviewer included |
|---|---|---|
| `django-api` | `backend`, `tester` | yes |
| `fastapi` | `backend`, `tester` | yes |
| `react-app` | `frontend`, `tester` | no |
| `next-js` | `frontend`, `tester` | no |
| `monorepo` | `backend`, `frontend`, `tester` | no |
| `data-pipeline` | `data`, `tester` | no |
| `cli-tool` | `backend`, `tester` | no |

After `init`, fill in `WORKFORCE.md` with your project context. See [WORKFORCE.md](#workforcemd).

---

### Roster

#### `workforce hire`

```bash
workforce hire NAME [OPTIONS]
```

Hire a new specialist into the roster. `NAME` must match `^[a-z][a-z0-9_-]{0,31}$`.

| Flag | Default | Description |
|---|---|---|
| `--role ROLE` | — | Role description (required unless `--from-template` provides one) |
| `--from-template TEMPLATE` | — | Seed from a built-in template; see [Specialist templates](#specialist-templates) |
| `--model MODEL` | `claude-sonnet-4-6` | Claude model id |

At least one of `--role` or `--from-template` is required. `--from-template` is the fast path: it populates the role, tool set, and base prompt from the template.

---

#### `workforce fire`

```bash
workforce fire NAME [OPTIONS]
```

Remove a specialist from the roster.

| Flag | Default | Description |
|---|---|---|
| `--yes`, `-y` | off | Skip confirmation |

> [!WARNING]
> `fire` permanently removes the specialist, ALL their cross-project memory, and ALL their stats. There is no undo.

---

#### `workforce roster`

```bash
workforce roster
```

List all specialists in a table: name, model, mission count, total cost, role.

---

#### `workforce show`

```bash
workforce show NAME
```

Show one specialist's full details: metadata, allowed tools, base prompt, cross-project memory, and per-project memory for every project the specialist is assigned to.

---

#### `workforce templates`

```bash
workforce templates
```

List all 11 built-in specialist templates with their role description and tool set. See [Specialist templates](#specialist-templates) for the full table.

---

#### `workforce refresh`

```bash
workforce refresh [NAME] [OPTIONS]
```

Re-apply the latest common preamble (commit policy, working style) to one or all specialists, preserving each specialist's `## Role` section.

| Flag | Default | Description |
|---|---|---|
| `NAME` | all specialists | Specialist to refresh; omit to refresh all |
| `--yes`, `-y` | off | Skip confirmation |

---

### Projects

#### `workforce project add`

```bash
workforce project add PATH [OPTIONS]
```

Register a directory as a Workforce project.

| Flag | Default | Description |
|---|---|---|
| `PATH` | required | Path to a git repo or plain directory (must exist) |
| `--name NAME` | directory basename | Display name |
| `--workspace` | off | Force workspace kind (no git operations) |
| `--repo` | off | Force repo kind (fails if no `.git`) |

`--workspace` and `--repo` are mutually exclusive. Auto-detection: `.git` present → `repo`; otherwise → `workspace`.

---

#### `workforce project assign`

```bash
workforce project assign PROJECT SPECIALIST [SPECIALIST...]
```

Assign one or more specialists to a project. Multiple specialist names can be listed in one command.

---

#### `workforce project unassign`

```bash
workforce project unassign PROJECT SPECIALIST
```

Remove a specialist from a project (one at a time).

---

#### `workforce project list`

```bash
workforce project list
```

List all registered projects with their IDs, kind, assigned specialists, and paths.

---

#### `workforce project show`

```bash
workforce project show PROJECT
```

Show project details: kind, path, assigned specialists, default model, recorded mission count, total cost. `PROJECT` may be a display name, 12-hex ID, or `.` to auto-detect from the current working directory.

---

#### `workforce project tail`

```bash
workforce project tail PROJECT [OPTIONS]
```

Stream events from **all** missions in a project, interleaved with specialist labels. New missions are picked up automatically. Exits once every known mission has emitted a `ResultMessage` and a grace period passes. Ctrl-C to detach at any time.

| Flag | Default | Description |
|---|---|---|
| `--show-thinking` | off | Include thinking blocks in output |
| `--poll FLOAT` | `0.5` | How often to check for new events (seconds) |
| `--all-done-timeout FLOAT` | `0` | Exit with error if all missions don't finish within N seconds (0 = disabled) |

---

#### `workforce project config`

```bash
workforce project config PROJECT
```

Show the active per-project configuration read from `.workforce.toml`. Fields absent from the file are shown with their effective defaults. `PROJECT` accepts display name, 12-hex ID, or `.`.

---

#### `workforce project forget`

```bash
workforce project forget PROJECT [OPTIONS]
```

Unregister a project — removes its record, per-project memory, and mission history from `~/.workforce`. Does not touch the repo itself or its `.workforce-project-id` marker file.

| Flag | Default | Description |
|---|---|---|
| `--yes`, `-y` | off | Skip confirmation |

> [!TIP]
> Use `forget` to deregister without losing the source code. Use `nuke` only when you want to erase ALL mission history including branches.

---

#### `workforce project nuke`

```bash
workforce project nuke PROJECT [OPTIONS]
```

Wipe all branches, worktrees, and mission artifacts for a project. Keeps the project registration itself and (by default) per-specialist project memory.

| Flag | Default | Description |
|---|---|---|
| `--also-memory` | off | Also wipe per-specialist project memory |
| `--dry-run` | off | Show what would be deleted without changing anything |
| `--yes`, `-y` | off | Skip confirmation |

> [!WARNING]
> `nuke` deletes all `workforce/*` branches and mission artifacts. This cannot be undone.

---

### Missions

#### `workforce dispatch`

```bash
workforce dispatch PROJECT [TICKET] [OPTIONS]
```

The primary command. Dispatch a mission on a project. The Manager plans it first (unless `--specialist` is passed), then the work runs.

`PROJECT` may be a display name, 12-hex ID, or `.` to auto-detect from cwd.

**Ticket sources** (exactly one must be provided):

| Source | How |
|---|---|
| Positional argument | `workforce dispatch myapp "do the thing"` |
| `--file PATH` | Read ticket from a Markdown file |
| `--stdin` | Read ticket from stdin |
| `--github-issue URL` | Fetch from a GitHub issue (`owner/repo#N` or full URL) |
| `--github-pr URL` | Fetch from a GitHub PR description |
| `$EDITOR` | Opened automatically when none of the above is provided (interactive only) |

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--specialist NAME` | — | Skip Manager; dispatch this specialist directly |
| `--auto-staff / --no-auto-staff` | on | Let Manager auto-hire/assign missing specialists |
| `--auto-merge / --no-auto-merge` | off | Merge mission branch after successful completion |
| `--merge-into BRANCH` | — | Branch to merge into after success |
| `--branch NAME` | — | Staging branch: fork from here, merge back here on success |
| `--max-turns N` | `50` | Hard cap on assistant turns per sub-mission |
| `--max-cost FLOAT` | `5.0` | Hard cap on cost (USD) per sub-mission |
| `--max-wall FLOAT` | `1800.0` | Hard cap on wall-clock seconds per sub-mission |
| `--max-retries N` | `0` | Retry failed sub-missions N times |
| `--retry-backoff FLOAT` | `30.0` | Base backoff seconds between retries |
| `--yes`, `-y` | off | Skip decomposition confirmation prompt |
| `--panels` | off | Show per-worker live panels (parallel mode only) |
| `--review` | off | Run Reviewer after each sub-mission; re-run on rejection |
| `--max-revisions N` | `3` | Max Reviewer rejection loops per sub-mission |
| `--window` | off | Background mission in a new OS terminal window. Requires `--specialist`. |
| `--background` | off | Background mission silently. Requires `--specialist`. |
| `--dry-run` | off | Plan only; print decomposition and estimated cost without running |
| `--ci` | off | Non-interactive: skip prompts, suppress ANSI, write JSON summary to stdout |
| `--output-file PATH` | — | With `--ci`: also write JSON summary to this path |
| `--require-review` | off | With `--ci`: fail (exit 2) if `--review` was not passed |
| `--github-issue URL` | — | Fetch ticket from GitHub issue URL |
| `--github-pr URL` | — | Fetch ticket from GitHub PR URL |
| `--open-pr` | off | After successful `--auto-merge`, open a GitHub PR |
| `--pr-base BRANCH` | `main` | Base branch for the GitHub PR |
| `--pr-draft` | off | Open the GitHub PR in draft mode (implies `--open-pr`) |

**Exit codes (CI mode):**

| Code | Meaning |
|---|---|
| `0` | Mission completed |
| `1` | Mission failed or errored |
| `2` | Review rejected |
| `4` | Manager error |

**CI JSON summary** (written to stdout, and optionally `--output-file`):

```json
{
  "mission_id": "m-20260512-120000-ab12",
  "status": "completed",
  "cost_usd": 0.8341,
  "branch": "workforce/m-20260512-120000-ab12",
  "commits": 3
}
```

For the full planning flow, see [Dispatch deep dive](#dispatch-deep-dive).

---

#### `workforce manage`

```bash
workforce manage PROJECT [OPTIONS]
```

Open an interactive Manager chat session. The Manager can dispatch workers, answer questions about ongoing/past missions, and maintain context across turns. Each dispatched mission uses `--window` (its own terminal).

| Flag | Default | Description |
|---|---|---|
| `--yolo` | off | Skip per-tool permission prompts (`bypassPermissions`) |
| `--branch NAME` | — | Staging branch; missions fork from it and merge back |

> [!WARNING]
> `--yolo` bypasses all tool-use confirmation in the Manager chat. Only use this when you fully trust the Manager's planned actions.

---

#### `workforce missions`

```bash
workforce missions PROJECT
```

List missions recorded for a project, newest first. Shows: mission id, kind, timestamp, specialist/tasks, status, cost, ticket preview.

---

#### `workforce stats`

```bash
workforce stats [OPTIONS]
```

Show mission statistics across all projects. Default output is a per-specialist Rich table.

| Flag | Default | Description |
|---|---|---|
| `--since DATE` | — | Filter to missions on or after this ISO date (`2026-05-01`) |
| `--by-project` | off | Pivot to per-project rows instead of per-specialist |
| `--csv` | off | Output CSV |
| `--json` | off | Output full `StatsResult` as JSON |

---

#### Mission inspection and management

**`workforce mission show`**

```bash
workforce mission show MISSION_ID
```

Show one mission's full details: metadata, ticket, result, commits. Works for single, parent (parallel), or sub-missions.

---

**`workforce mission tail`**

```bash
workforce mission tail MISSION_ID [OPTIONS]
```

Pretty-print a mission's `events.jsonl` as it's appended (live tail).

| Flag | Default | Description |
|---|---|---|
| `--show-thinking` | off | Include thinking blocks |
| `--follow / --no-follow`, `-f` | on | Keep watching for new events |
| `--poll FLOAT` | `0.5` | How often to check for new events (seconds) |
| `--timeout FLOAT` | `0` | Exit with error if mission doesn't finish within N seconds (0 = disabled) |

---

**`workforce mission diff`**

```bash
workforce mission diff MISSION_ID [OPTIONS]
```

Show `git diff {base_sha}..HEAD` in the mission's worktree. For parallel parent missions, shows a labelled diff per sub-mission.

| Flag | Default | Description |
|---|---|---|
| `--stat` | off | Show diffstat instead of full diff |

---

**`workforce mission retry`**

```bash
workforce mission retry MISSION_ID [OPTIONS]
```

Re-dispatch a past mission with the same ticket and specialist.

| Flag | Default | Description |
|---|---|---|
| `--background` | off | Background the re-dispatch |

---

**`workforce mission clean`**

```bash
workforce mission clean MISSION_ID [OPTIONS]
```

Drop the mission's worktree (and its registry entry). Keeps artifacts (`events.jsonl`, `result.md`, `meta.json`) and the branch.

| Flag | Default | Description |
|---|---|---|
| `--force`, `-f` | off | Remove worktree even with uncommitted changes |
| `--yes`, `-y` | off | Skip confirmation |

> [!NOTE]
> `clean` does not delete the mission record or branch — only the worktree directory. Use it to reclaim disk space while preserving history.

---

**`workforce mission prune`**

```bash
workforce mission prune [OPTIONS]
```

Bulk-remove old mission worktrees. Mission logs and branches are kept.

| Flag | Default | Description |
|---|---|---|
| `--older-than DURATION` | `30d` | Remove worktrees older than this (e.g. `7d`, `24h`, `2w`, `1m`) |
| `--dry-run` | off | List what would be removed without touching anything |
| `--keep-failed` | off | Don't prune failed mission worktrees |

Duration units: `h` hours, `d` days, `w` weeks, `m` 30-day months.

---

**`workforce replay`**

```bash
workforce replay MISSION_ID [OPTIONS]
```

Pretty-print a mission's `events.jsonl` to the terminal. Useful for auditing what a specialist did turn-by-turn.

| Flag | Default | Description |
|---|---|---|
| `--show-thinking` | off | Include thinking blocks |

---

### Branches

#### `workforce branches prune`

```bash
workforce branches prune PROJECT [OPTIONS]
```

Delete merged `workforce/*` branches (and their worktrees) from a project.

| Flag | Default | Description |
|---|---|---|
| `--into BRANCH` | current branch | Compare merge status against this branch |
| `--dry-run` | off | List without deleting |
| `--yes`, `-y` | off | Skip confirmation |

---

### Memory

#### `workforce memory show`

```bash
workforce memory show SPECIALIST
```

Print a summary table of memory files for a specialist: scope, path, line count, and approximate token count (chars ÷ 4).

---

#### `workforce memory search`

```bash
workforce memory search SPECIALIST QUERY [OPTIONS]
```

Search specialist memory files (case-insensitive). Prints matching lines with 2 lines of context.

| Flag | Default | Description |
|---|---|---|
| `--project PROJECT` | — | Also search per-project memory for this project |

---

#### `workforce memory export`

```bash
workforce memory export SPECIALIST [OPTIONS]
```

Print specialist memory to stdout with `# Cross-project memory` / `# Project memory: <name>` section headers. Redirect to a file to persist or transfer.

| Flag | Default | Description |
|---|---|---|
| `--project PROJECT` | — | Also export per-project memory for this project |

---

#### `workforce memory import`

```bash
workforce memory import SPECIALIST [OPTIONS]
```

Replace a memory file with the contents of a local file.

| Flag | Default | Description |
|---|---|---|
| `--file PATH`, `-f PATH` | required | File to import |
| `--project PROJECT` | — | Import into per-project memory for this project |
| `--cross-project` | off | Explicitly import into cross-project memory |
| `--yes`, `-y` | off | Skip confirmation |

---

#### `workforce memory compact`

```bash
workforce memory compact SPECIALIST [OPTIONS]
```

Compact a specialist's memory file using the AI model (single-turn summarisation). Feeds the current memory to the model and writes the condensed result back.

| Flag | Default | Description |
|---|---|---|
| `--project PROJECT` | — | Compact per-project memory instead of cross-project |
| `--keep-last N` | — | Preserve last N lines verbatim; compact only older content |
| `--threshold-tokens N` | — | Skip if memory is under N tokens (chars ÷ 4) |
| `--yes`, `-y` | off | Skip confirmation |

> [!NOTE]
> Memory is append-only and grows without bound. Compact specialist memory periodically — monthly for active specialists, or when `workforce show <name>` reports a large memory file.

See [Memory system](#memory-system) for a full explanation of how memory is captured and when to run compaction.

---

### Ticket templates

#### `workforce ticket new`

```bash
workforce ticket new [TYPE] [OPTIONS]
```

Create a new ticket from a template and open it in `$EDITOR`. After the editor closes, Workforce prints the content and asks whether to dispatch it immediately. The temp file path is printed for manual dispatch later.

| Flag | Default | Description |
|---|---|---|
| `TYPE` | prompted | Ticket type (see table below) |
| `--list`, `-l` | off | List available ticket types and exit |

**Available ticket types:**

| Type | Scaffolded sections |
|---|---|
| `bug-fix` | Bug, Steps to reproduce, Expected behaviour, Actual behaviour, Likely files involved |
| `feature` | Feature description, Acceptance criteria (checkboxes), Out of scope, Likely files involved |
| `refactor` | What to refactor, Why (motivation), Constraints, Test coverage required |
| `chore` | Task description, Done when (acceptance with checkboxes), Notes |
| `docs` | What to document, Audience, Format, Related files |

Manual dispatch from the saved file:

```bash
workforce dispatch myapp --file /tmp/workforce-ticket-XXXX.md
```

---

### Config

#### `workforce config get`

```bash
workforce config get
```

Print the current global configuration from `~/.workforce/config.toml` as a table. Shows which config file is being read.

---

#### `workforce config set`

```bash
workforce config set KEY VALUE
```

Set a key in the global config file, creating it if needed.

Supported keys: `default_model` (string), `max_turns` (integer), `max_cost` (float).

See [Configuration reference](#configuration-reference) for the full precedence explanation.

---

### Specialist marketplace

The marketplace is a shared registry of community specialist definitions hosted at `workforce-ai/specialists` on GitHub.

#### `workforce specialist search`

```bash
workforce specialist search [QUERY] [OPTIONS]
```

Search the marketplace (fetches `index.json` from the registry). Omit `QUERY` to list all available specialists.

| Flag | Default | Description |
|---|---|---|
| `--registry-url URL` | `https://raw.githubusercontent.com/workforce-ai/specialists/main` | Registry base URL |

---

#### `workforce specialist install`

```bash
workforce specialist install SLUG [OPTIONS]
```

Download and install a specialist from the marketplace.

| Flag | Default | Description |
|---|---|---|
| `SLUG` | required | Registry slug (e.g. `backend-go`) |
| `--name NAME` | prompted (default: slug) | Local install name |
| `--registry-url URL` | default registry | Registry base URL |

> [!NOTE]
> Memory and stats are never exported or imported. Only `specialist.toml` is included in marketplace entries.

---

#### `workforce specialist publish`

```bash
workforce specialist publish NAME [OPTIONS]
```

Export a local specialist for publishing to the marketplace. Writes `specialist.toml` and a `README.md` stub to the output directory.

| Flag | Default | Description |
|---|---|---|
| `NAME` | required | Specialist name in local roster |
| `--output-dir PATH` | `./specialists/<name>/` | Output directory |

Submission workflow: publish locally → copy to a fork of `workforce-ai/specialists` → open a pull request. See [Specialist marketplace (advanced)](#specialist-marketplace-1) for the full contributing flow.

---

### Webhook daemon

Requires `pip install 'workforce-ai[webhook]'`.

```bash
workforce webhook start [--port 8080] [--host 0.0.0.0] [--config PATH]
workforce webhook status
workforce webhook stop
```

Listens for GitHub webhook events and auto-dispatches missions. See [Webhook daemon (advanced)](#webhook-daemon-advanced) for the full configuration reference, security notes, and production setup.

---

### Web dashboard

Requires `pip install 'workforce-ai[web]'`.

```bash
workforce serve [--port 8080] [--host 127.0.0.1] [--reload]
```

Starts a local browser dashboard at `http://127.0.0.1:8080/` showing active and past missions, per-specialist stats, and the current roster. Read-only: all mutations go through the CLI. See [Web dashboard (advanced)](#web-dashboard-advanced) for the full route reference.

---

### MCP server

Requires `pip install 'workforce-ai[mcp]'`.

```bash
workforce mcp-server
```

Starts the Workforce MCP server on stdio (JSON-RPC 2.0). Exposes dispatch, roster, mission status, and mission result as MCP tools callable directly from Claude Code. See [MCP server (advanced)](#mcp-server-advanced) for tool schemas and Claude Code configuration.

---

## Dispatch deep dive

### How the Manager plans

`workforce dispatch` runs the **Manager** first on every call unless `--specialist` bypasses it. The Manager reads the project source (using Read/Glob/Grep — never Write or Edit), reads `WORKFORCE.md` when present, and outputs a **Decomposition** with one of three kinds:

- **`single`** — one specialist handles the entire ticket in one worktree.
- **`parallel`** — independent sub-tasks with declared `owns_paths` file-ownership lanes; all run concurrently within a dependency wave.
- **`sequential`** — fully ordered chain; each task merges its dependency branch before the next starts.

**Lane enforcement (parallel mode):** At plan time the Manager validates that declared `owns_paths` patterns are non-overlapping. At runtime, the `can_use_tool` callback blocks any write outside the specialist's declared lane. After each sub-mission, Workforce checks which files were actually changed vs which paths were declared — the audit result appears in `meta.json`.

> [!TIP]
> To skip Manager for a simple, well-scoped ticket, use `--specialist <name>`. This also skips the decomposition confirmation prompt.

---

### Decomposition confirmation

After the Manager plans, the CLI prints a decomposition table and prompts:

```
Proceed? [y]es / [n]o / [d]iscuss with Manager
```

- `y` — accept the plan and start running.
- `n` — abort.
- `d` — replan loop: the Manager receives your feedback and the prior decomposition and produces a revised one. You can loop as many times as needed.
- `--yes` / `-y` — skip the prompt entirely.

> [!NOTE]
> The `d` (discuss) option requires an interactive terminal. It does not work in `--background` or `--ci` mode.

---

### Auto-staffing

When the Manager proposes a task and suggests a specialist role you haven't hired yet (via a `template_hint`), Workforce **auto-staffs**: it hires a specialist from the template and assigns them to the project automatically.

Disable with `--no-auto-staff` to get a hard error instead when a required role is missing.

---

### Reviewer loop

`--review` enables a post-mission Reviewer pass after each sub-mission:

1. The Reviewer (read-only + Bash) diffs `base_sha..HEAD`, runs tests/linters, and returns `approved: true/false`.
2. On rejection, the original specialist re-runs with the Reviewer's feedback as additional context.
3. The loop is capped at `--max-revisions N` rounds (default `3`).
4. If the loop exhausts without approval, mission status becomes `review_rejected`.

Reviewer costs are billed separately and tracked as `review_cost_usd` in `meta.json`.

> [!WARNING]
> `--review` is not available on workspace projects — there is no git diff to inspect.

---

### Staging branch

`--branch dev` keeps work isolated on a named staging branch:

- The `dev` branch is created from current HEAD if it doesn't exist.
- Mission worktrees fork from `dev`.
- On successful completion, the mission branch is auto-merged back into `dev`.
- `main` is never touched.

Use `--auto-merge --merge-into <branch>` to merge into a non-default target without using the `--branch` shorthand.

---

### Detached dispatch

For long-running missions you don't want to babysit:

- `--window`: spawns the mission in a new OS-level terminal window. Returns immediately.
- `--background`: runs the mission in a silent child process. No window.

Follow up with `workforce mission tail <id>` or `workforce project tail <project>` to watch output.

> [!WARNING]
> Both `--window` and `--background` require `--specialist`. Manager-driven parallel dispatch cannot be detached — the Manager runs interactively.

---

## Project kinds

| Capability | Repo project | Workspace project |
|---|---|---|
| Git worktrees | ✅ One per mission | ❌ Runs in project directory |
| Branch isolation | ✅ `workforce/<mission-id>` | ❌ No branches |
| Commits | ✅ Specialist commits as it goes | ❌ No git operations |
| `--review` | ✅ | ❌ Requires git diff |
| `--auto-merge` | ✅ | ❌ |
| `--branch` | ✅ | ❌ |
| Parallel path-lane enforcement | ✅ | ✅ |
| When to use | Engineering / code changes | Non-git tasks, document generation, data pipelines |

**Registering a repo project:**

```bash
workforce project add ~/code/myapp          # auto-detected from .git presence
workforce project add ~/code/myapp --repo   # force repo kind
```

**Registering a workspace project:**

```bash
workforce project add ~/docs/content --workspace
```

---

## WORKFORCE.md

`WORKFORCE.md` is a project-level context file that the Manager reads before decomposing every ticket. It is the single highest-leverage thing you can create for a project.

> [!TIP]
> `WORKFORCE.md` is the single highest-leverage file you can create. A focused 200-word file beats a sprawling 2000-word one — the Manager has a token budget.

**How to create it:**

```bash
# Fastest: generated from a stack template
workforce init --template fastapi

# Or create manually — place WORKFORCE.md in the repo root
```

**Recommended sections:**

```markdown
# WORKFORCE.md

Brief project description (1–2 sentences).

## Specialist Roster

| Name      | Role                        |
|-----------|-----------------------------|
| `backend` | Senior backend engineer.    |
| `tester`  | Test engineer.              |

## Common Tickets

Recurring ticket patterns so the Manager can anticipate them.

## Project Notes

Stack versions, framework conventions, auth approach, linting rules.

## Build & Test

```bash
uv pip install -e '.[dev]'
pytest -q
```

## Deployment

CI pipeline, platform, required environment variables.
```

**Stack templates** auto-generate `WORKFORCE.md` with stack-specific placeholders. All 7 templates are available via `workforce init --list`.

**Tips:**

- Keep it under ~200–300 words. The Manager reads it on every dispatch.
- Update it when tooling changes (test command, deployment platform, new specialist added).
- Commit it to the repo — every specialist assigned to the project will read it.

---

## Configuration reference

### Global config (`~/.workforce/config.toml`)

Created and updated by `workforce config set`. Read by `workforce config get` and applied as defaults to every `dispatch` call.

All fields are optional. A missing or malformed file is silently ignored.

| Key | Type | Default | Description |
|---|---|---|---|
| `default_model` | string | `null` | Override the Claude model for all specialists |
| `max_turns` | integer | `null` | Default turn cap per mission |
| `max_cost` | float | `null` | Default USD spend cap per mission |

Example:

```toml
default_model = "claude-opus-4-6"
max_turns = 30
max_cost = 3.00
```

Manage with the CLI:

```bash
workforce config get
workforce config set max_turns 30
workforce config set max_cost 3.00
workforce config set default_model "claude-opus-4-6"
```

Or edit `~/.workforce/config.toml` directly.

---

### Per-project config (`.workforce.toml`)

Place in the project's repo root. Created automatically by `workforce init --template <name>`. Read by `workforce dispatch` and `workforce project config`.

All fields are optional. CLI flags always take precedence over this file.

| Key | Type | Default | Description |
|---|---|---|---|
| `default_specialist` | string | `null` | Bypass the Manager and dispatch this specialist by default |
| `review` | boolean | `null` | Enable the Reviewer loop by default |
| `auto_merge` | boolean | `null` | Auto-merge completed branches by default |
| `max_turns` | integer | `null` | Per-project default turn cap |
| `max_cost` | float | `null` | Per-project default cost cap (USD) |

Example:

```toml
default_specialist = "backend"
review = true
auto_merge = false
max_turns = 40
max_cost = 2.50
```

---

### Precedence

Highest to lowest — the first matching value wins:

1. **CLI flag** (e.g. `--max-turns 80`)
2. **`.workforce.toml`** in the project root (per-project override)
3. **`~/.workforce/config.toml`** (global defaults)
4. **RunLimits built-in defaults** (`max_turns=50`, `max_cost=5.0`, `max_wall=1800s`)

---

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `WORKFORCE_HOME` | `~/.workforce` | Root of all Workforce on-disk state |
| `ANTHROPIC_API_KEY` | (required) | Anthropic API key used by specialist subprocesses |
| `WORKFORCE_WEBHOOK_CONFIG` | `~/.workforce/webhook.toml` | Path to the webhook daemon config file |
| `EDITOR` | `nano` | Editor opened by `workforce dispatch` (no args) and `workforce ticket new` |

---

### Project marker file

When a project is registered, Workforce writes a `.workforce-project-id` file in the repo root containing the 12-hex project ID. Commit this file. It lets Workforce resolve the correct project record even if the repo is moved to a different absolute path — the ID derived from the path would change, but the marker file still resolves the record.

---

## Memory system

### Two memory scopes

**Cross-project memory** — `~/.workforce/roster/<name>/memory.md`

Lessons that apply across any project: tool quirks, debugging strategies, workflow patterns. Injected into the specialist's system prompt as `<cross_project_memory>…</cross_project_memory>`.

**Per-project memory** — `~/.workforce/projects/<id>/memory/<specialist>.md`

Quirks specific to one repo: where tests live, the build command, framework conventions, authentication approach. Injected as `<project_memory>…</project_memory>`.

### How memory is captured

After each successful mission, one extra SDK turn asks the specialist to produce a structured JSON with `summary`, `project_memory`, and `cross_project_memory` fields. Workforce parses this via a fenced-code-block regex and appends to the appropriate memory files under a `## <mission-id>` heading.

Failure is silent — the mission completes regardless, and `memory_delta_captured: false` is recorded in `meta.json` when extraction fails.

### Memory growth and compaction

Both files are append-only and grow unboundedly. Large memory files slow context loading because more tokens are consumed per prompt.

- Run `workforce memory compact <specialist>` to summarize and prune old entries using the AI model.
- Run it monthly for active long-lived specialists, or when `workforce show <name>` reports an unexpectedly large memory file.
- `--threshold-tokens N` skips compaction if the file is already small.
- `--keep-last N` preserves the most recent N lines verbatim.

> [!NOTE]
> For new specialists on a fresh project, memory is empty and grows gradually. The first few missions cost slightly less than later ones as context accumulates — this is expected and intentional.

---

## Specialist templates

11 built-in templates. Seed a new specialist with `workforce hire NAME --from-template TEMPLATE`.

| Template | Role | Tool set | Read-only? |
|---|---|---|---|
| `backend` | Senior backend engineer | Read, Write, Edit, Bash, Glob, Grep, WebFetch | No |
| `frontend` | Senior frontend engineer | Read, Write, Edit, Bash, Glob, Grep, WebFetch | No |
| `tester` | Test engineer | Read, Write, Edit, Bash, Glob, Grep | No |
| `reviewer` | Code reviewer | Read, Bash, Glob, Grep | **Yes** |
| `generalist` | Generalist engineer | Read, Write, Edit, Bash, Glob, Grep, WebFetch | No |
| `devops` | DevOps / platform engineer | Read, Write, Edit, Bash, Glob, Grep, WebFetch | No |
| `data` | Data engineer / analyst | Read, Write, Edit, Bash, Glob, Grep | No |
| `docs` | Technical writer | Read, Write, Edit, Glob, Grep | No |
| `security` | Security engineer | Read, Bash, Glob, Grep | **Yes** |
| `db` | Database engineer | Read, Write, Edit, Bash, Glob, Grep | No |
| `mobile` | Mobile application engineer | Read, Write, Edit, Bash, Glob, Grep | No |

> [!WARNING]
> `reviewer` and `security` are read-only templates — no Write or Edit tools. They cannot modify files. Use them for audit and inspection tasks, not implementation.

`docs` also omits Bash and WebFetch. For full tool lists, use `workforce templates`.

**Custom specialists:** Use `workforce hire <name> --role "…" --model "…"` and optionally `--tools TOOL [TOOL …]` to create a fully custom specialist without a template.

**Specialist name rules:** `^[a-z][a-z0-9_-]{0,31}$` — lowercase, max 32 characters.

---

## Mission artifacts

All files live at `~/.workforce/projects/<project-id>/missions/<mission-id>/`.

| File | Written at | Content |
|---|---|---|
| `ticket.md` | Mission start | Verbatim ticket text |
| `events.jsonl` | Continuously during run | JSONL stream of all SDK messages (flushed immediately) |
| `transcript.md` | Mission end | Human-readable assistant turns only, separated by `---` |
| `result.md` | Mission end | Best summary: memory-delta summary → last assistant text → `"(no summary captured)"` |
| `meta.json` | Mission end (atomic write) | Full structured mission record |
| `decomposition.json` | After Manager runs | Manager's decomposition plan (JSON) |
| `startup.log` | Background dispatch | Subprocess stderr (`--window` / `--background` only) |
| `stderr.log` | During run | claude CLI diagnostic stderr |

### `meta.json` key fields

| Field | Type | Description |
|---|---|---|
| `mission_id` | string | `m-YYYYMMDD-HHMMSS-xxxx` |
| `status` | string | `running`, `completed`, `error`, `wall_timeout`, `interrupted`, `review_rejected` |
| `specialist` | string | Specialist name |
| `cost_usd` | float | Total cost including Manager and Reviewer rounds |
| `manager_cost_usd` | float | Manager planning cost (0 when `--specialist` used) |
| `review_cost_usd` | float | Sum of all Reviewer rounds |
| `branch` | string\|null | `workforce/<mission-id>`; `null` for workspace |
| `base_sha` | string\|null | Git SHA the worktree forked from |
| `commits` | list | Git commits: `{sha, subject, body}` |
| `turn_count` | integer | Number of assistant turns |
| `revision_rounds` | integer | Times specialist re-ran in response to Reviewer |
| `memory_delta_captured` | bool | Whether post-mission memory extraction succeeded |
| `started_at` / `ended_at` | string | ISO-8601 UTC timestamps |
| `duration_seconds` | float | Wall-clock time |

For the full `meta.json` and `events.jsonl` schema, see [`docs/events-schema.md`](docs/events-schema.md).

### `events.jsonl` types

Each line is one JSON object with a `_type` discriminator:

| `_type` | Key fields |
|---|---|
| `SystemMessage` | `subtype="init"`, session id, tools list |
| `AssistantMessage` | `content[]` (TextBlock/ToolUseBlock/ThinkingBlock), model, cost |
| `UserMessage` | `content[]` (ToolResultBlock/TextBlock), `parent_tool_use_id` |
| `ResultMessage` | `subtype`, `is_error`, `num_turns`, `total_cost_usd`, `duration_ms` |

**Live tailing:** `workforce mission tail <id>` streams events as they arrive. Because events are flushed per-line, `tail -f ~/.workforce/projects/<id>/missions/<mission-id>/events.jsonl` also works.

---

## Commit policy

Specialists commit their own work as they go, in the mission's worktree branch. Commits follow conventional-commits style (`feat`, `fix`, `refactor`, `docs`, `chore`, etc.), are authored as the repo's `user.name`/`user.email` — Workforce never overrides git identity — and include a `Co-Authored-By: <specialist-name> <specialist-name>@workforce.local` trailer to credit which specialist did the work.

---

## Webhook daemon (advanced)

### Install

```bash
pip install 'workforce-ai[webhook]'
```

### Configuration (`~/.workforce/webhook.toml`)

Override the config path with `WORKFORCE_WEBHOOK_CONFIG`.

**Top-level fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `secret` | string | required | GitHub webhook HMAC-SHA256 secret |
| `dispatch_label` | string | `"workforce-dispatch"` | Issue label that triggers dispatch |
| `auto_review` | bool | `false` | Auto-dispatch Reviewer on every opened PR |
| `projects` | list | `[]` | Repo-to-project mappings |

**`[[projects]]` entry fields:**

| Field | Type | Default | Description |
|---|---|---|---|
| `repo` | string | required | GitHub `owner/repo` |
| `project` | string | required | Workforce project name or ID |
| `specialist` | string | `null` | Bypass Manager; dispatch this specialist instead |

**Example:**

```toml
secret = "replace-with-your-github-webhook-secret"
dispatch_label = "workforce-dispatch"
auto_review = false

[[projects]]
repo = "acme/backend"
project = "backend"

[[projects]]
repo = "acme/frontend"
project = "frontend"
specialist = "senior-engineer"
```

### Daemon lifecycle

```bash
# Start the daemon (blocks; binds 0.0.0.0:8080 by default)
workforce webhook start
workforce webhook start --port 9000 --host 127.0.0.1
workforce webhook start --config /path/to/webhook.toml

# Check if running (reads ~/.workforce/webhook.pid)
workforce webhook status

# Graceful shutdown (sends SIGTERM)
workforce webhook stop
```

### Supported GitHub events

| Event | Action | Trigger condition | What happens |
|---|---|---|---|
| `issues` | `labeled` | Label matches `dispatch_label` | Dispatch mission with issue title+body as ticket |
| `pull_request` | `opened` | `auto_review = true` | Dispatch Reviewer with PR title+body as ticket |
| `ping` | — | Webhook creation | Responds `200 OK` |

### HTTP endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhook` | GitHub webhook receiver (HMAC-verified) |
| `GET` | `/health` | Returns `{"status": "ok"}` |

### Security

Every incoming request is HMAC-SHA256 verified via the `X-Hub-Signature-256` header using `hmac.compare_digest` (timing-safe). Requests with missing or incorrect signatures return `400` / `401`.

> [!WARNING]
> Keep `webhook.toml` readable only by the service user (`chmod 600 ~/.workforce/webhook.toml`). The secret proves GitHub sent the request — anyone who has it can trigger arbitrary mission dispatch.

> [!TIP]
> Run behind nginx or Caddy with TLS in production. GitHub always sends webhook events to HTTPS endpoints.

### Production: systemd unit

```ini
[Unit]
Description=Workforce webhook daemon
After=network.target

[Service]
ExecStart=/usr/local/bin/workforce webhook start --port 8080
Restart=on-failure
EnvironmentFile=/etc/workforce/env
WorkingDirectory=/var/lib/workforce

[Install]
WantedBy=multi-user.target
```

---

## GitHub Actions

Use the `wyennie/workforce` composite action to dispatch missions from your CI/CD pipeline.

### Prerequisites

- Python 3.11+ available in the runner
- A registered Workforce project at the path passed as `project`
- An `ANTHROPIC_API_KEY` secret in your repository or organization

### Step 1: Add the secret

In your repository: **Settings → Secrets and variables → Actions → New repository secret**. Name it `ANTHROPIC_API_KEY`. For organization-wide use, add it as an organization secret and grant access to the relevant repositories.

> [!WARNING]
> Always pass `anthropic-api-key` from a repository secret, never hardcoded in the workflow file. GitHub Actions secrets are masked in logs.

### Step 2: Reference the action

```yaml
# Option A: pin to a release tag (recommended for production)
- uses: wyennie/workforce@v1

# Option B: local reference (when Workforce is vendored in the repo)
- uses: ./
```

### Step 3: Configure inputs

| Input | Required | Default | Description |
|---|---|---|---|
| `project` | yes | — | Path to the registered project directory |
| `ticket` | one of `ticket`/`ticket-file` | — | Free-text ticket |
| `ticket-file` | one of `ticket`/`ticket-file` | — | Path to a Markdown file containing the ticket |
| `specialist` | no | auto | Specific specialist name; omit to let Manager decide |
| `auto-merge` | no | `'false'` | `'true'` to merge the mission branch on success |
| `open-pr` | no | `'false'` | `'true'` to open a GitHub PR for the mission branch |
| `max-cost` | no | — | USD spend cap (e.g. `'2.50'`) |
| `anthropic-api-key` | yes | — | Pass as `${{ secrets.ANTHROPIC_API_KEY }}` |

Exactly one of `ticket` or `ticket-file` must be provided.

### Step 4: Read outputs

| Output | Description |
|---|---|
| `mission-id` | Unique mission identifier (`m-YYYYMMDD-HHMMSS-xxxx`) |
| `status` | Terminal status: `completed`, `failed`, `errored`, or `review_rejected` |
| `branch` | Git branch created for the mission |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Completed |
| `1` | Failed / errored |
| `2` | Review rejected |
| `4` | Manager error |

### Minimal example workflow

```yaml
# .github/workflows/dispatch.yml
name: Workforce dispatch

on:
  workflow_dispatch:
    inputs:
      ticket:
        description: "Ticket for the specialist"
        required: true

jobs:
  dispatch:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Dispatch mission
        id: wf
        uses: wyennie/workforce@v1
        with:
          project: ${{ github.workspace }}
          ticket: ${{ github.event.inputs.ticket }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          auto-merge: 'true'

      - name: Print mission info
        if: always()
        run: |
          echo "Mission: ${{ steps.wf.outputs.mission-id }}"
          echo "Status:  ${{ steps.wf.outputs.status }}"
          echo "Branch:  ${{ steps.wf.outputs.branch }}"
```

### Tips

- **Cost control:** Set `max-cost: '1.50'` to cap per-workflow spend.
- **Selective dispatch:** Use `if: contains(github.event.issue.labels.*.name, 'workforce')` to trigger only on labelled issues.
- **Staging branch:** Combine `auto-merge: 'true'` with a `--branch dev` wrapper script to collect all CI-triggered work on one staging branch.

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `AuthenticationError` in logs | `ANTHROPIC_API_KEY` not set or expired | Check secret name and key validity |
| `Project not found` | `project` path is not registered with Workforce | Run `workforce project add <path>` or `workforce init` in the repo |
| `Specialist not found` | `specialist` input references an unhired specialist | `workforce hire <name>` first, or omit `specialist` to let Manager auto-staff |
| Exit code `4` | Manager failed to plan | Check `--no-auto-staff` isn't set; ensure `WORKFORCE.md` exists and is well-formed |

---

## MCP server (advanced)

The Workforce MCP server exposes dispatch and inspection capabilities as tools that any MCP-compatible AI assistant can call.

### What is MCP?

The [Model Context Protocol](https://spec.modelcontextprotocol.io/) is an open standard for connecting AI assistants to external tools and data sources. The Workforce MCP server makes it possible to dispatch missions and inspect results without leaving a Claude Code session.

### Install

```bash
pip install 'workforce-ai[mcp]'
```

### Start

```bash
workforce mcp-server
```

Protocol: stdio, JSON-RPC 2.0 (newline-delimited). No network port.

### Four tools

**`workforce_dispatch`** — Dispatch a mission. Blocks until complete.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | string | yes | Project name or ID |
| `ticket` | string | yes | Task description |
| `specialist` | string | no | Specialist name (auto-selected if omitted) |
| `auto_merge` | boolean | no | Merge branch on completion |

Returns: `{"mission_id": "...", "status": "completed", "branch": "..."}` or `{"error": "..."}`.

---

**`workforce_mission_status`** — Read a mission's `meta.json`.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `mission_id` | string | yes | Mission ID (`m-YYYYMMDD-HHMMSS-xxxx`) |

Returns: Full `meta.json` dict or `{"error": "mission <id> not found"}`.

---

**`workforce_roster`** — List all specialists. No parameters.

Returns: Array of `{"name", "role", "missions", "cost_usd"}`.

---

**`workforce_mission_result`** — Fetch a mission's result summary.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `mission_id` | string | yes | Mission ID |

Returns: Raw Markdown text of `result.md`, or an error string.

### Claude Code configuration

**Global** (`~/.claude/claude.json`):

```json
{
  "mcpServers": {
    "workforce": {
      "command": "workforce",
      "args": ["mcp-server"],
      "env": {
        "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"
      }
    }
  }
}
```

**Project-level** (`.claude/claude.json` in repo root): same structure.

### Authentication model

No auth layer. The server relies on `ANTHROPIC_API_KEY` in the environment and process-level access control — it inherits the caller's filesystem permissions.

> [!WARNING]
> Never expose the stdio server over a network socket without adding your own authentication wrapper. The server inherits the caller's full filesystem access.

---

## Web dashboard (advanced)

### Install

```bash
pip install 'workforce-ai[web]'
```

### Start

```bash
workforce serve                              # http://127.0.0.1:8080/
workforce serve --port 3000
workforce serve --host 0.0.0.0 --port 8080  # bind to all interfaces
workforce serve --reload                    # dev mode: auto-reload on code changes
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--port N` | `8080` | TCP port |
| `--host H` | `127.0.0.1` | IP address to bind |
| `--reload` | off | Enable auto-reload (development mode) |

### Routes

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Mission list with optional filters (`project`, `status`, `since`) |
| `GET` | `/mission/{mission_id}` | Mission detail: ticket, result, metadata |
| `GET` | `/mission/{mission_id}/diff` | Git diff viewer (`git diff {base_sha}..HEAD`) |
| `GET` | `/mission/{mission_id}/events` | Server-Sent Events stream of `events.jsonl` |
| `GET` | `/stats` | Cost by specialist, missions per day (last 30 days), success rates |
| `GET` | `/roster` | All specialists with mission counts, cost, memory size |

### Features

- Filter missions by project, status, since-date
- Color-coded status badges: running (blue), completed (green), review issues (yellow), error (red)
- Git diff viewer with syntax highlighting
- Live SSE stream for in-progress missions
- Stats page: per-specialist cost/count/success-rate, daily mission counts

The dashboard is **read-only** — all mutations go through the CLI.

---

## Developing Workforce

```bash
git clone git@github.com:wyennie/workforce.git
cd workforce
uv venv && source .venv/bin/activate
uv pip install -e '.[dev]'
workforce doctor
```

> [!TIP]
> `pip install -e '.[dev]'` (plain pip) also works if you prefer not to use uv.

Lint, type-check, and test:

```bash
ruff check        # lint
mypy              # type check (strict)
pytest -q         # run test suite (~5 seconds, 440+ tests)
```

Source layout: all production code under `src/workforce/`. Tests: `tests/`, one file per module. CI: `.github/workflows/ci.yml`.

See [ARCHITECTURE.md](ARCHITECTURE.md) for a deep dive into how Workforce dispatches missions, manages worktrees, and handles the reviewer loop.

---

## License

MIT — see [LICENSE](LICENSE).
