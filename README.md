# Workforce

A CLI that gives you a persistent roster of Claude specialists, assignable across projects, dispatchable on tickets, and runnable in parallel via git worktrees.

Workforce is a staffing agency for AI engineers, not a session manager. You hire named specialists (`aria` the backend specialist, `ben` the frontend specialist, …), assign them to projects, and file tickets. Workforce picks the right specialist, isolates the work in a git worktree, runs a `claude-agent-sdk` subprocess to completion, and writes back what was learned so the next mission starts smarter.

## Install

**Recommended** — install from PyPI as an isolated tool (no virtualenv management needed):

```bash
uv tool install workforce-ai
# or: pipx install workforce-ai
workforce doctor
```

For reproducible installs, pin a version:

```bash
uv tool install workforce-ai==0.1.0
```

**Fallback — one-liner** (installs [`uv`](https://docs.astral.sh/uv/) if missing, then installs
from the GitHub repo HEAD):

```bash
curl -LsSf https://raw.githubusercontent.com/wyennie/workforce/main/install.sh | bash
```

To install a specific tagged release via the script:

```bash
curl -LsSf https://raw.githubusercontent.com/wyennie/workforce/main/install.sh | bash -s -- --tag v0.1.0
# List available versions:
curl -LsSf https://raw.githubusercontent.com/wyennie/workforce/main/install.sh | bash -s -- --list-tags
```

Or install directly from git if you prefer:

```bash
uv tool install git+https://github.com/wyennie/workforce
# or:  pip install git+https://github.com/wyennie/workforce
workforce doctor
```

`doctor` verifies Python, the `claude-agent-sdk` package, the `claude` CLI, git, auth, and that your Workforce home directory is writable. Run it before anything else.

## Quickstart

```bash
workforce hire aria --from-template backend
workforce hire ben  --from-template frontend

workforce project add ~/code/myapp
workforce project assign myapp aria ben

# File a ticket. The Manager decides whether to fan out across specialists
# or hand it to one; auto-staffs from templates if a needed role is missing.
workforce dispatch myapp "Add a /health endpoint returning build SHA and uptime"

# Or open an interactive Manager chat and iterate:
workforce manage myapp
```

Each mission runs in its own worktree under `~/.workforce/projects/<id>/worktrees/<mission-id>/` on a `workforce/<mission-id>` branch. Workforce streams events live, writes a `result.md`, and prints the merge plan. Nothing lands on `main` until you say so — pass `--auto-merge` (or `--branch dev` for a staging branch that auto-merges into `dev`) when you're ready to remove that step.

## Layout

State lives at `~/.workforce/` by default. Override with `WORKFORCE_HOME=/path/to/dir`.

```
~/.workforce/
├── config.toml
├── roster/<specialist>/
│   ├── specialist.toml
│   ├── memory.md          # cross-project lessons (append-only)
│   └── stats.json
└── projects/<project-id>/
    ├── project.toml
    ├── memory/<specialist>.md
    ├── worktrees/<mission-id>/
    └── missions/<mission-id>/
        ├── ticket.md
        ├── events.jsonl     # full replay log
        ├── result.md
        ├── transcript.md
        └── meta.json
```

## Commands

```
# Setup
workforce init [--template NAME] [--blank] [--list] [--demo]  # scaffold a new project
workforce doctor                       # environment check

# Roster
workforce hire <name> [--role ...] [--from-template backend|frontend|tester|reviewer|generalist]
workforce fire <name>
workforce roster
workforce show <name>
workforce templates
workforce refresh                      # re-apply latest preamble to existing specialists

# Projects
workforce project add <path> [--name <name>]
workforce project assign <project> <specialist>...
workforce project unassign <project> <specialist>
workforce project list
workforce project show <project>
workforce project forget <project> -y  # remove registration + memory; leaves the repo alone
workforce project nuke <project> -y    # remove all branches, worktrees, and mission history. Irreversible.

# Missions
workforce dispatch <project> "<ticket>" [--specialist <name>] [--branch <name>] [--auto-merge] [--review]
workforce manage <project> [--branch <name>] [--yolo]   # interactive Manager chat; --yolo skips per-tool prompts
workforce stats [--project P] [--specialist S] [--since DATE] [--json]  # aggregated mission stats
workforce missions <project>
workforce mission show <id>
workforce mission clean <id>
workforce mission prune --older-than 30d
workforce replay <id>

# Branches
workforce branches prune <project> [--into main] [--dry-run] [-y]

# Config
workforce config get
workforce config set <key> <value>

# Memory
workforce memory show <specialist>
workforce memory search <specialist> <query>
workforce memory export <specialist>
workforce memory import <specialist> <file>
workforce memory compact <specialist>

# Ticket templates
workforce ticket new [TYPE]            # scaffold from template and open in editor
workforce ticket new --list            # list available types

# Specialist marketplace
workforce specialist search [QUERY]                         # browse the registry
workforce specialist install <slug> [--name <name>]         # download and install
workforce specialist publish <name> [--output-dir DIR]      # export for marketplace PR

# Webhook daemon  (requires [webhook] extras)
workforce webhook start [--port 8080] [--host 0.0.0.0] [--config FILE]
workforce webhook status
workforce webhook stop

# Web dashboard  (requires [web] extras)
workforce serve [--port 8080] [--host 127.0.0.1] [--reload]

# MCP server  (requires [mcp] extras)
workforce mcp-server
```

### How dispatch decides

`workforce dispatch <project> "<ticket>"` runs the **Manager** first (a built-in role, not a hireable specialist). The Manager reads the project, picks `kind = parallel | sequential | single`, and proposes a decomposition. You confirm before anything runs. For tiny tickets, `--specialist <name>` skips the Manager entirely.

When the Manager picks `parallel` or `sequential`, each task gets its own worktree, branch, and subprocess. Tasks run in dependency order (Kahn's-algorithm topological sort); independent tasks run concurrently. If the Manager wants a specialty you haven't hired yet (and provides a `template_hint`), Workforce **auto-staffs**: it hires from a template and assigns to the project. Disable with `--no-auto-staff`.

### Staging branch (`--branch`)

Pass `--branch dev` on `dispatch` or `manage` to keep work on a staging branch. Mission worktrees fork from `dev`, and on success the work is auto-merged back into `dev`. The branch is created from current HEAD if it doesn't exist, and `main` is never touched — review what landed on `dev` and promote it manually.

### Reviewer loop (`--review`)

With `--review`, after each sub-mission a Reviewer specialist (read-only) inspects the diff. On rejection, the original specialist re-runs with the Reviewer's feedback. Capped at `--max-revisions` rounds (default 3).

### Manager chat (`manage`)

`workforce manage <project>` opens an interactive conversation with the Manager. Two notable options:

- `--yolo` — skips all per-tool permission prompts in the Manager chat (sets `bypassPermissions`). Use only when you fully trust the Manager's actions; the default is to confirm before each tool call.
- `--branch <name>` — same staging-branch semantics as `dispatch --branch`.

### Workspace project kind

A _workspace_ project (`workforce project add <path> --kind workspace`) differs from a normal git project:

- **No git isolation**: missions run directly in the workspace directory; there are no worktrees or branches.
- **`--review`, `--auto-merge`, and `--branch` are not available** — all specialists share the working directory, so branching is not applicable.
- Useful for projects that are not version-controlled or where you want specialists to coordinate in a single directory.

### Memory growth

Each specialist accumulates a memory file that grows over time as missions complete. Large memory files slow context loading. Periodically compact it once tooling is available:

```
workforce memory compact <name>   # coming soon
```

## Project initialisation (`workforce init`)

`workforce init` registers the current directory as a Workforce project, writes a `WORKFORCE.md` template, and optionally hires a preconfigured set of specialists in one step.

```bash
# See available stack templates
workforce init --list

# Scaffold with a template (hires specialists, writes .workforce.toml)
workforce init --template fastapi
workforce init --template react-app --name my-app

# Register without any specialists
workforce init --blank

# Spin up a toy calculator demo to try Workforce with no setup
workforce init --demo
```

Available stack templates: `django-api`, `fastapi`, `react-app`, `next-js`, `monorepo`, `data-pipeline`, `cli-tool`. Each template hires the right specialist roles and seeds `WORKFORCE.md` with project-specific hints.

After init, fill in `WORKFORCE.md` with your project context — the Manager reads it before decomposing every ticket.

## Web dashboard (`workforce serve`)

`workforce serve` starts a local browser dashboard. Requires the `[web]` extras:

```bash
pip install 'workforce-ai[web]'
workforce serve            # http://127.0.0.1:8080/
workforce serve --port 3000 --host 0.0.0.0
workforce serve --reload   # dev mode: auto-reloads on code changes
```

The dashboard shows active and past missions, per-specialist stats, and the current roster. It is read-only; all mutations go through the CLI.

## Specialist Marketplace

The specialist marketplace is a shared registry of community-contributed specialist definitions. Browse, install, and contribute without leaving the CLI.

**Search and install:**

```bash
# Browse the full registry
workforce specialist search

# Filter by keyword
workforce specialist search go
workforce specialist search "frontend react"

# Install a specialist (you'll be prompted for a local name)
workforce specialist install backend-go
workforce specialist install backend-go --name go-api

# Assign to a project
workforce project assign myapp go-api
```

**Publish your own specialist:**

```bash
# Export a local specialist for submission
workforce specialist publish aria --output-dir ./specialists/aria
```

This writes `specialist.toml` and a `README.md` stub. Fork [workforce-ai/specialists](https://github.com/workforce-ai/specialists), copy the directory under `specialists/`, add an entry to `specialists/index.json`, and open a pull request.

Memory and stats are never included in exports — they are local runtime artifacts.

## Webhook daemon

The webhook daemon listens for GitHub webhook events (push, pull request, workflow run, etc.) and triggers Workforce actions automatically. Requires the `[webhook]` extras:

```bash
pip install 'workforce-ai[webhook]'
```

**Configuration** — create `~/.workforce/webhook.toml` (or point to a custom path with `--config`):

```toml
secret = "your-github-webhook-secret"

[[routes]]
event = "push"
ref   = "refs/heads/main"
project = "myapp"
specialist = "backend"
```

**Manage the daemon:**

```bash
workforce webhook start                          # bind 0.0.0.0:8080
workforce webhook start --port 9000 --host 127.0.0.1
workforce webhook start --config /path/to/webhook.toml

workforce webhook status   # check if running, show PID
workforce webhook stop     # send SIGTERM
```

The daemon writes its PID to `~/.workforce/webhook.pid`. Point your GitHub repo's webhook at `http://<host>:<port>/webhook` with the same secret from the config file.

## Ticket templates (`workforce ticket`)

`workforce ticket new` opens a structured ticket in your `$EDITOR` and optionally dispatches it when you save.

```bash
# List available types
workforce ticket new --list

# Create a ticket interactively (prompts for type if omitted)
workforce ticket new
workforce ticket new bug-fix
workforce ticket new feature
```

Available types: `bug-fix`, `feature`, `refactor`, `chore`, `docs`. Each template is pre-filled with the relevant sections (steps to reproduce, acceptance criteria, etc.) as Markdown comment placeholders. Edit, save, quit — Workforce shows the content and asks whether to dispatch immediately. The temp file path is printed so you can dispatch manually:

```bash
workforce dispatch myapp --file /tmp/workforce-ticket-XXXX.md
```

## MCP server (`workforce mcp-server`)

`workforce mcp-server` starts an [MCP](https://spec.modelcontextprotocol.io/) server on stdio, exposing Workforce capabilities (dispatch, project list, roster, mission status) as tools that Claude Code can call directly. Requires the `[mcp]` extras:

```bash
pip install 'workforce-ai[mcp]'
```

Add to `~/.claude/claude.json` to make Workforce available in every Claude Code session:

```json
{
  "mcpServers": {
    "workforce": {
      "command": "workforce",
      "args": ["mcp-server"]
    }
  }
}
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for a detailed description of how Workforce dispatches missions, manages worktrees, and handles the reviewer loop.

## Configuration

Global defaults live in `~/.workforce/config.toml`. All keys are optional; CLI flags always take precedence.

```toml
default_model = "claude-sonnet-4-5"
max_turns = 80
max_cost  = 10.0
```

Manage it with the built-in subcommands:

```bash
workforce config get                  # show current settings
workforce config set max_turns 80     # write a value
workforce config set max_cost 10.0
workforce config set default_model "claude-sonnet-4-5"
```

Or edit `~/.workforce/config.toml` directly.

## Commit policy

Specialists commit their own work as they go, in the worktree's branch. Conventional-commits style, authored as you (Workforce never overrides `user.name`/`user.email`). A per-specialist `Co-Authored-By: <name> <name>@workforce.local` trailer credits which specialist did the work.

## Develop

For working on Workforce itself, use an editable install so changes in `src/` are live:

```bash
git clone git@github.com:wyennie/workforce.git
cd workforce
uv venv && source .venv/bin/activate
uv pip install -e '.[dev]'
workforce doctor
```

Plain `python3 -m venv .venv && pip install -e '.[dev]'` works too. Lint, type-check, and test:

```bash
ruff check
mypy
pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
