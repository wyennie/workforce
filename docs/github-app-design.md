# Workforce GitHub App — Design Document

This document describes the architecture and design of a first-party GitHub App
that integrates Workforce with GitHub without requiring per-repository workflow
files or manual secret management.

---

## 1. OAuth App vs GitHub App

### Why GitHub App (not OAuth App)

| Dimension | OAuth App | GitHub App |
|-----------|-----------|------------|
| Identity | Acts as the *user* | Acts as a *bot* — separate identity |
| Installation scope | Per-user | Per-repository or per-organization |
| Token lifetime | Long-lived user token | Short-lived installation tokens (1 h) |
| Rate limits | Shared with the authorizing user | Own rate limit bucket (5 000–15 000 req/h) |
| Permissions | All-or-nothing OAuth scopes | Fine-grained, per-resource permissions |
| Webhook delivery | Not supported natively | First-class; GitHub retries failed deliveries |
| Secret management | Owner must share token | Centrally managed; installations can be revoked |

**Decision: GitHub App.**

The Workforce App dispatches autonomous missions that push code, open PRs, and
post check runs. These operations benefit from a dedicated bot identity
(comments are attributed to `workforce[bot]` rather than a human account),
fine-grained permissions so the app never holds write access beyond what it
needs, and short-lived installation tokens that are automatically rotated.
Webhook delivery is essential to the event-driven dispatch model.

---

## 2. Required Permissions

The app requests the minimum set of permissions needed for its three core
operations: read the trigger, do the work, report the result.

| Resource | Permission | Reason |
|----------|-----------|--------|
| **Issues** | Read | Read issue body and labels to build the mission ticket |
| **Pull requests** | Write | Post review comments and create PRs for completed missions |
| **Checks** | Write | Report mission status as a GitHub check run with annotations |
| **Contents** | Read | Clone the repository into the mission worktree |
| **Contents** | Write | Push the `workforce/<mission-id>` branch after completion |
| **Metadata** | Read | Required by GitHub for all apps |

*Contents: Write* is scoped to branches matching `workforce/*` via branch
protection rules when the app configures a new installation — it never touches
`main` or `dev` directly.

---

## 3. Webhook Events

| Event | Action filter | Handler |
|-------|--------------|---------|
| `issues` | `labeled` | Dispatch a mission when label is `workforce-dispatch` |
| `pull_request` | `opened` | Optionally dispatch a reviewer specialist |
| `push` | — | Update mission state if the workforce branch is force-pushed or amended |

### Event payload → mission ticket

```
issues.labeled
  └── issue.body          → ticket text
  └── issue.labels[*].name → routing hints (e.g. 'backend', 'reviewer')
  └── issue.title         → prepended as a one-line summary

pull_request.opened
  └── pull_request.title  → review subject
  └── pull_request.body   → additional context
  └── pull_request.diff_url → fetched and embedded in the ticket

push (workforce/* branch)
  └── head_commit.message → update meta.json with the latest commit SHA
```

Webhook signatures are verified using the `X-Hub-Signature-256` header and a
per-installation HMAC secret stored server-side.

---

## 4. State Model — Installation → Workforce Project

Each GitHub App installation maps to exactly one Workforce project. This
one-to-one relationship is stored in the **installation registry**:

```
installations/
├── <installation-id>.json
│   ├── installation_id   (GitHub numeric ID)
│   ├── account_login     (org or user who installed)
│   ├── repository_ids[]  (repos in scope, if not org-wide)
│   ├── workforce_project_id   (maps to ~/.workforce/projects/<id>/)
│   ├── workforce_home    (filesystem path on the runner host)
│   ├── anthropic_api_key_ref  (reference to secret vault key)
│   └── created_at
```

### Lifecycle

```
install event → create installation record
                → prompt maintainer for Anthropic API key via OAuth flow
                → store key in vault
                → register project with Workforce if not already registered

uninstall event → revoke API key from vault
                 → mark installation record inactive
                 → optionally prune worktrees older than 30 days

suspend event  → stop accepting new webhook payloads for this installation
```

### Mapping webhook payload → project

```python
def resolve_project(installation_id: int) -> WorkforceProject:
    record = installation_registry.get(installation_id)
    if record is None:
        raise UnknownInstallation(installation_id)
    return WorkforceProject.load(record.workforce_project_id)
```

---

## 5. Result Reporting

Workforce missions report results through three channels, chosen based on the
triggering event:

### 5a. GitHub Check Runs (primary)

Check runs appear directly on commits and PRs. The app creates a check run when
the mission starts and updates it when the mission finishes.

```
POST /repos/{owner}/{repo}/check-runs
  name        : "Workforce / <specialist-name>"
  head_sha    : <commit that triggered the workflow>
  status      : in_progress → completed
  conclusion  : success | failure | neutral | action_required
  output.title   : "Mission <mission-id> completed"
  output.summary : first 500 chars of result.md
  output.annotations : per-file line-level findings (reviewer mode)
```

### 5b. Issue Comments (issue-triggered missions)

When a mission is triggered by `issues.labeled`, the app posts a summary
comment when the mission finishes:

```markdown
## Workforce mission completed ✅

| Field      | Value                            |
|------------|----------------------------------|
| Mission ID | `m-20260512-120000-abcd`         |
| Status     | `completed`                      |
| Branch     | `workforce/m-20260512-120000-abcd` |

[View full transcript →](https://workforce.example.com/missions/m-20260512-120000-abcd)
```

### 5c. PR Reviews (PR-triggered missions)

When a reviewer specialist finishes, the app submits a formal GitHub PR review
via `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews` with
`event: COMMENT | REQUEST_CHANGES | APPROVE` based on the specialist's verdict.

---

## 6. Multi-Tenant Considerations

### Per-repository vs per-organization installation

The app supports both installation scopes:

| Scope | Use case | Notes |
|-------|----------|-------|
| **Per-repository** | Single project, tight control | One installation record per repo |
| **Per-organization** | Many repos, shared specialist roster | One installation, multiple project mappings |

For org installations the app dynamically creates a Workforce project for each
new repository that receives a `workforce-dispatch` label event, using the org's
shared `ANTHROPIC_API_KEY` secret.

### Isolation guarantees

- Each repository gets its own `WORKFORCE_HOME` subdirectory (`~/.workforce/projects/<hash>/`).
- Mission worktrees are created under the project home and cleaned up after merge.
- Anthropic API keys are stored per-installation in the vault; no installation
  can read another installation's key.
- Runner hosts are isolated per-tenant (separate VMs or containers) in the
  managed deployment.

### Rate limit budget

Each installation uses its own GitHub App token (1-hour lifetime) with an
independent rate limit bucket. High-volume orgs that dispatch many missions
concurrently can request a higher rate limit through the GitHub Enterprise
program.

---

## 7. Installation Flow (Step-by-Step User Journey)

```
1. User clicks "Install" on the Workforce App GitHub Marketplace page.

2. GitHub prompts the user to choose an account (personal or org) and
   which repositories to grant access to.

3. GitHub sends an `installation.created` webhook to the Workforce server.

4. The Workforce server responds with a redirect to the onboarding portal:
   https://workforce.example.com/onboard?installation_id=<id>

5. The onboarding portal authenticates the user via GitHub OAuth
   (read:user scope only, for identity verification).

6. The user pastes their Anthropic API key into the onboarding form.
   The key is validated by making a test API call (list models).

7. The server stores the key in the vault and links it to the installation ID.

8. The server registers a Workforce project for each selected repository
   by running `workforce project add <clone-path>` on the runner host.

9. The user sees a success screen with a badge and a webhook delivery log.

10. Future webhook events (issues.labeled, pull_request.opened) are
    routed to the correct installation and dispatch Workforce missions.
```

---

## 8. Architecture Diagram

```
                           ┌─────────────────────────────────────────────────┐
                           │                   GitHub                         │
                           │                                                  │
                           │  ┌──────────┐  label    ┌─────────────────────┐ │
                           │  │  Issue   │──────────▶│  Webhook delivery   │ │
                           │  └──────────┘           └──────────┬──────────┘ │
                           │                                     │            │
                           │  ┌──────────┐  opened              │            │
                           │  │    PR    │──────────────────────┘            │
                           │  └──────────┘  POST /webhook                    │
                           └─────────────────────┬───────────────────────────┘
                                                 │  HTTPS + HMAC-SHA256
                                                 ▼
                           ┌─────────────────────────────────────────────────┐
                           │              Workforce App Server                │
                           │                                                  │
                           │  ┌──────────────────────────────────────────┐   │
                           │  │           Webhook Router                  │   │
                           │  │  verify signature → parse event type      │   │
                           │  │  resolve installation → Workforce project  │   │
                           │  └──────────────────┬───────────────────────┘   │
                           │                     │                            │
                           │          ┌──────────┴──────────┐                │
                           │          │                     │                 │
                           │          ▼                     ▼                 │
                           │  ┌──────────────┐    ┌─────────────────┐        │
                           │  │ Mission Queue│    │ Installation DB │        │
                           │  │  (Redis/SQS) │    │  (PostgreSQL)   │        │
                           │  └──────┬───────┘    └────────┬────────┘        │
                           │         │                     │                  │
                           │         │  dequeue            │ install_id →    │
                           │         ▼                     │ project_id,     │
                           │  ┌──────────────┐            │ api_key_ref      │
                           │  │  Runner Host │◀───────────┘                  │
                           │  │              │                                │
                           │  │  $ workforce │                                │
                           │  │    dispatch  │                                │
                           │  │    --ci      │                                │
                           │  └──────┬───────┘                                │
                           │         │  result.json                           │
                           │         ▼                                         │
                           │  ┌──────────────────────────────────────────┐   │
                           │  │           Result Reporter                 │   │
                           │  │  POST check-run update (checks API)       │   │
                           │  │  POST issue comment (issues API)          │   │
                           │  │  POST PR review (pulls API)               │   │
                           │  └──────────────────────────────────────────┘   │
                           └─────────────────────────────────────────────────┘
                                                 │
                                                 │ GitHub API calls
                                                 │ (installation token)
                                                 ▼
                           ┌─────────────────────────────────────────────────┐
                           │                   GitHub                         │
                           │  Check run updated / comment posted / PR review  │
                           └─────────────────────────────────────────────────┘


Key:
  ──▶  event flow
  ◀──  data fetch
  $ workforce dispatch  runs as a subprocess on the runner host
```

---

## 9. Security Considerations

- **Webhook signature verification** is mandatory on every inbound request.
  Requests without a valid `X-Hub-Signature-256` header are rejected with 401.
- **Installation tokens** are generated on-demand (POST /app/installations/{id}/access_tokens)
  and never cached beyond 55 minutes to avoid using expired tokens.
- **Anthropic API keys** are stored in a dedicated secrets vault (HashiCorp Vault
  or AWS Secrets Manager), never in the database or logs.
- **Runner isolation**: each mission runs in an ephemeral container with only the
  target repository cloned. No cross-tenant filesystem access.
- **Least-privilege**: the app never requests `administration`, `workflows`, or
  `secrets` permissions. Contents:Write is scoped to the `workforce/*` branch
  namespace via the installation's repository configuration.

---

## 10. Open Questions / Future Work

| Topic | Question |
|-------|---------|
| Self-hosted runners | Should the app support customer-owned runners for air-gapped environments? |
| Caching | Can we cache the Workforce roster between missions to avoid re-reading TOML on every run? |
| Cost reporting | Post the USD cost of each mission as a check run annotation? |
| Audit log | Expose a per-installation mission history API for compliance requirements |
| Billing | Token-based pricing per specialist-minute, or flat subscription? |
