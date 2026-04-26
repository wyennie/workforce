# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Phase 1–5 features consolidated into the 0.1.0 baseline (see below)

---

## [0.1.0] - 2026-05-12

### Added

- **Roster management** — `hire`, `fire`, `roster`, `show`, `templates`, `refresh` commands;
  built-in specialist templates (backend, frontend, tester, reviewer, generalist, data)
- **Project management** — `project add/assign/unassign/list/show/forget`; git-repo and
  plain-directory project kinds; per-project memory files
- **Mission dispatch** — `dispatch` command with Manager decomposition (parallel, sequential,
  single); topological task ordering; auto-staffing from templates when a needed role is absent
- **Parallel execution** — independent sub-tasks run concurrently in separate git worktrees;
  results merged back to the target branch
- **Reviewer loop** — `--review` flag adds a read-only Reviewer pass after each sub-mission;
  rejection triggers a retry with feedback, capped at `--max-revisions` rounds
- **Interactive manager chat** — `manage` command; streaming SDK events; prompt_toolkit REPL
- **Staging branch support** — `--branch <name>` on dispatch/manage; work stays isolated until
  manually promoted
- **CI mode** — `--ci` flag suppresses prompts and writes a JSON result file; exit codes encode
  outcome (0 completed, 1 failed, 2 review rejected, 4 manager error)
- **Background dispatch** — `--background` detaches the mission to a subprocess and returns the
  mission ID immediately
- **Mission lifecycle** — `missions`, `mission show`, `mission clean`, `mission prune`,
  `mission tail`, `replay`; atomic meta.json writes; per-mission events log
- **Branch pruning** — `branches prune` with `--into` merge target and `--dry-run`
- **Stats and aggregation** — `stats` command; cached aggregation across all projects/missions
- **Config management** — `config get/set/list`; TOML config at `~/.workforce/config.toml`;
  `WORKFORCE_HOME` env-var override
- **Doctor command** — checks Python version, `claude-agent-sdk`, `claude` CLI, git, auth,
  and writable home directory
- **GitHub Actions CI** — matrix test on Python 3.11/3.12/3.13 with ruff, mypy, pytest
- **Release workflow** — automated build, PyPI publish, and GitHub Release on `v*.*.*` tags
- **Versioned installer** — `install.sh` with `--tag` and `--list-tags` support
- **Distribution packaging** — PyPI distribution as `workforce-ai`; `uv tool install` and
  `pipx install` recommended install paths
