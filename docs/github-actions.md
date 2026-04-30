# Workforce GitHub Actions Integration

This document explains how to use the `action.yml` composite action to dispatch
Workforce missions from GitHub Actions workflows.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.11+ on the runner | `ubuntu-latest` ships with 3.12 |
| Git repository registered with Workforce | `workforce project add <path>` |
| Anthropic API key | Required by every specialist subprocess |

---

## Step 1 — Add the `ANTHROPIC_API_KEY` secret

1. Open your repository on GitHub.
2. Go to **Settings → Secrets and variables → Actions**.
3. Click **New repository secret**.
4. Name: `ANTHROPIC_API_KEY`, value: your Anthropic API key.
5. Click **Add secret**.

For organization-wide workflows, add the secret at the organization level and
grant access to the target repositories.

---

## Step 2 — Reference the action in a workflow

The action lives at the root of the Workforce repository (`action.yml`). You
can reference it in two ways:

### Option A — Pin to a release tag (recommended for production)

```yaml
- uses: wyennie/workforce@v1
  with:
    project: ${{ github.workspace }}
    ticket: "Add input validation to the /register endpoint"
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

### Option B — Local reference (when Workforce is vendored in your repo)

```yaml
- uses: ./  # action.yml at the repo root
  with:
    project: ${{ github.workspace }}
    ticket: "Add input validation to the /register endpoint"
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

---

## Step 3 — Configure inputs

All inputs are passed under the `with:` key.

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `project` | Yes | — | Path to the project directory registered with Workforce |
| `ticket` | One of these | — | Free-text ticket for the mission |
| `ticket-file` | One of these | — | Path to a Markdown file containing the ticket |
| `specialist` | No | auto | Name of a specific specialist; omit to let the Manager decide |
| `auto-merge` | No | `false` | Merge the mission branch into the base branch on success |
| `open-pr` | No | `false` | Open a GitHub PR for the mission branch on success |
| `max-cost` | No | — | USD spend cap (e.g. `'2.50'`); mission aborts if exceeded |
| `anthropic-api-key` | Yes | — | Anthropic API key; always pass from a secret |

Exactly one of `ticket` or `ticket-file` must be provided.

---

## Step 4 — Read outputs

After the action runs, three outputs are available:

| Output | Description |
|--------|-------------|
| `mission-id` | Unique mission identifier (e.g. `m-20260512-120000-abcd`) |
| `status` | Terminal status: `completed`, `failed`, `errored`, or `review_rejected` |
| `branch` | Git branch created for the mission (e.g. `workforce/m-20260512-…`) |

Reference outputs with `${{ steps.<step-id>.outputs.<output-name> }}`.

Exit codes from the action map to workflow step failure automatically:

| Exit code | Meaning |
|-----------|---------|
| 0 | Mission completed successfully |
| 1 | Mission failed or errored |
| 2 | Review rejected (specialist output did not pass the reviewer loop) |
| 4 | Manager error (decomposition or planning failed) |

---

## Minimal Example Workflow

```yaml
# .github/workflows/dispatch.yml
name: Workforce dispatch

on:
  workflow_dispatch:
    inputs:
      ticket:
        description: "Describe the task"
        required: true

jobs:
  dispatch:
    runs-on: ubuntu-latest
    permissions:
      contents: write
      pull-requests: write

    steps:
      - uses: actions/checkout@v4

      - name: Dispatch mission
        id: wf
        uses: wyennie/workforce@v1
        with:
          project: ${{ github.workspace }}
          ticket: ${{ github.event.inputs.ticket }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}

      - name: Show result
        run: |
          echo "Mission : ${{ steps.wf.outputs.mission-id }}"
          echo "Status  : ${{ steps.wf.outputs.status }}"
          echo "Branch  : ${{ steps.wf.outputs.branch }}"
```

---

## How the action works internally

1. **Install** — `pip install workforce-ai` pulls the latest release.
2. **Dispatch** — Runs `workforce dispatch <project> <ticket> --ci --output-file <tmp>`.
   The `--ci` flag disables interactive prompts and confirmation steps; the
   `--output-file` flag writes structured JSON on completion.
3. **Parse** — A short Python snippet reads the JSON and writes `mission-id`,
   `status`, and `branch` to `$GITHUB_OUTPUT`.

The action sets `if: always()` on the parse step so outputs are available even
when the dispatch step exits non-zero, letting you build conditional follow-up
steps (e.g. posting a failure comment on a PR).

---

## Tips

- **Cost control**: Set `max-cost: '1.00'` on automated triggers (issue labels,
  PR events) to prevent runaway spend.
- **Selective dispatch**: Use `if: contains(github.event.issue.labels.*.name, 'workforce-dispatch')`
  so not every issue triggers a mission.
- **Reviewers**: Pass `specialist: reviewer` to force the mission to a
  read-only reviewer specialist without touching production code.
- **Staging branch**: Set `auto-merge: true` together with a `--branch dev`
  config to accumulate completed missions on a staging branch.
- **Secrets in forks**: By default GitHub does not expose secrets to workflows
  triggered from forked PRs. Require approval for first-time contributors under
  Settings → Actions → Fork pull request workflows.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `mission-id` output is empty | `workforce dispatch` crashed before writing the output file | Check the "Dispatch mission" step log for a Python traceback or auth error |
| `status: errored` | Specialist subprocess failed | Run `workforce mission show <mission-id>` locally to replay the transcript |
| Permission denied pushing the mission branch | Runner lacks `contents: write` | Add `permissions: contents: write` to the job |
| `workforce: command not found` | pip install failed | Ensure the runner has Python 3.11+ and network access to PyPI |
