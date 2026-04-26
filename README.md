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

# Missions
workforce dispatch <project> "<ticket>" [--specialist <name>] [--branch <name>] [--auto-merge] [--review]
workforce manage <project> [--branch <name>] [--yolo]   # interactive Manager chat
workforce missions <project>
workforce mission show <id>
workforce mission clean <id>
workforce mission prune --older-than 30d
workforce replay <id>

# Branches
workforce branches prune <project> [--into main] [--dry-run] [-y]
```

### How dispatch decides

`workforce dispatch <project> "<ticket>"` runs the **Manager** first (a built-in role, not a hireable specialist). The Manager reads the project, picks `kind = parallel | sequential | single`, and proposes a decomposition. You confirm before anything runs. For tiny tickets, `--specialist <name>` skips the Manager entirely.

When the Manager picks `parallel` or `sequential`, each task gets its own worktree, branch, and subprocess. Tasks run in dependency order (Kahn's-algorithm topological sort); independent tasks run concurrently. If the Manager wants a specialty you haven't hired yet (and provides a `template_hint`), Workforce **auto-staffs**: it hires from a template and assigns to the project. Disable with `--no-auto-staff`.

### Staging branch (`--branch`)

Pass `--branch dev` on `dispatch` or `manage` to keep work on a staging branch. Mission worktrees fork from `dev`, and on success the work is auto-merged back into `dev`. The branch is created from current HEAD if it doesn't exist, and `main` is never touched — review what landed on `dev` and promote it manually.

### Reviewer loop (`--review`)

With `--review`, after each sub-mission a Reviewer specialist (read-only) inspects the diff. On rejection, the original specialist re-runs with the Reviewer's feedback. Capped at `--max-revisions` rounds (default 3).

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
