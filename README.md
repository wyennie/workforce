# Workforce

A CLI that gives you a persistent roster of Claude specialists, assignable across projects, dispatchable on tickets, runnable in parallel via git worktrees.

## Status

v0.1 in progress. See `WORKFORCE_BRIEF.md` for the full design and `DECISIONS.md` for implementation choices.

## Install (dev)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
workforce doctor
```

`doctor` verifies Python, the `claude-agent-sdk` package, the `claude` CLI, git, auth, and that your Workforce home directory is writable. Run it before anything else.

## Layout

State lives at `~/.workforce/` by default. Override with `WORKFORCE_HOME=/path/to/dir`.

```
~/.workforce/
├── config.toml
├── roster/<specialist>/
│   ├── specialist.toml
│   ├── memory.md          # cross-project lessons
│   └── stats.json
└── projects/<project-id>/
    ├── project.toml
    ├── memory/<specialist>.md
    └── missions/<mission-id>/
        ├── ticket.md
        ├── events.jsonl
        ├── result.md
        ├── transcript.md
        └── meta.json
```

## Commands (planned for v0.1)

```
workforce doctor                       # environment check  (done)
workforce hire <name> [--from-template ...]
workforce fire <name>
workforce roster
workforce show <name>

workforce project add <path>
workforce project assign <project> <specialist>...
workforce project unassign <project> <specialist>
workforce project list
workforce project show <project>

workforce dispatch <project> "<ticket>" [--specialist <name>]
workforce missions <project>
workforce mission <id>
workforce replay <id>
workforce mission clean <id>
workforce missions prune --older-than <duration>
```
