# WORKFORCE.md Convention

`WORKFORCE.md` is a project-level context file that Workforce reads before
decomposing any ticket. Placing one in the root of your repository lets the
Manager (the planning step before specialists are dispatched) understand the
project without having to rediscover it from scratch on every run.

## How it works

When `workforce dispatch` runs with `--parallel` (or any Manager-driven
flow), the Manager checks for `WORKFORCE.md` in the project root. If the
file is present, the Manager's planning prompt is prefixed with:

> Note: This project has a WORKFORCE.md — read it before decomposing the
> ticket.

The Manager then reads the file early in its planning pass, so the context
inside it shapes how the ticket is sliced and which specialists are chosen.

## Creating WORKFORCE.md

The quickest way to create a `WORKFORCE.md` is via `workforce init`:

```sh
# From inside your project directory
workforce init --template fastapi
# or
workforce init --blank
```

`workforce init` scaffolds the file with section placeholders based on the
chosen stack template and the hired specialist roster.

## Anatomy of WORKFORCE.md

A typical `WORKFORCE.md` has five sections:

```markdown
# WORKFORCE.md

Brief description of the project.

## Specialist Roster

| Name       | Role                                         |
|------------|----------------------------------------------|
| `backend`  | Senior backend engineer.                     |
| `tester`   | Test engineer.                               |

## Common Tickets

Describe recurring ticket patterns so the Manager can anticipate them.

## Project Notes

Stack-specific context: framework versions, database setup, auth approach,
coding conventions the specialists must follow.

## Build & Test

How to install dependencies and run the test suite.

## Deployment

CI pipeline, deployment platform, environment variables needed.
```

Fill in the sections that matter for your project; leave the rest as
comments. The Manager reads the whole file, so even partial context helps.

## Stack templates

`workforce init --list` shows all available stack templates:

| Template       | Default specialists          | Use case                          |
|----------------|------------------------------|-----------------------------------|
| `django-api`   | backend, tester, reviewer    | Django REST API                   |
| `fastapi`      | backend, tester, reviewer    | FastAPI service (async or sync)   |
| `react-app`    | frontend, tester             | React single-page app             |
| `next-js`      | frontend, tester             | Next.js full-stack app            |
| `monorepo`     | backend, frontend, tester    | Monorepo with multiple packages   |
| `data-pipeline`| data, tester                 | ETL / data pipeline project       |
| `cli-tool`     | backend, tester              | Command-line tool / Python package|

Each template also writes a `.workforce.toml` with a `stack` key so future
tooling can key off the project archetype.

## Tips

- **Keep it short.** The Manager has a token budget. A focused 200-word
  `WORKFORCE.md` beats a sprawling 2000-word one.
- **Update it as the project evolves.** Add the new test command when you
  switch test runners; update the deployment section when you move to a new
  platform.
- **Commit it.** `WORKFORCE.md` belongs in source control alongside your
  code. Every specialist that runs on the project will see it.
