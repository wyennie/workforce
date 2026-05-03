# Workforce Webhook Daemon

The webhook daemon listens for GitHub events and automatically dispatches
Workforce missions. Label an issue → a specialist picks it up. Open a PR →
the Reviewer runs. No manual `workforce dispatch` needed.

## Installation

Install the optional webhook dependencies alongside Workforce:

```bash
pip install 'workforce-ai[webhook]'
```

This adds [FastAPI](https://fastapi.tiangolo.com/) and
[uvicorn](https://www.uvicorn.org/) to the environment.

## Configuration

Create `~/.workforce/webhook.toml` (or point to another path via the
`WORKFORCE_WEBHOOK_CONFIG` environment variable):

```toml
# The shared secret you configured in GitHub's webhook settings.
# KEEP THIS PRIVATE — it proves GitHub sent the request.
secret = "replace-with-your-github-webhook-secret"

# Label that triggers automatic dispatch (default: "workforce-dispatch").
dispatch_label = "workforce-dispatch"

# Set to true to auto-dispatch a Reviewer on every opened pull request.
auto_review = false

# Map GitHub repos to Workforce projects.
[[projects]]
repo = "acme/backend"       # GitHub "owner/repo"
project = "backend"         # Workforce project name or id

[[projects]]
repo = "acme/frontend"
project = "frontend"
specialist = "senior-engineer"   # optional: bypass Manager, use this specialist
```

### Config fields

| Field | Type | Default | Description |
|---|---|---|---|
| `secret` | string | required | GitHub webhook shared secret |
| `dispatch_label` | string | `"workforce-dispatch"` | Issue label that triggers dispatch |
| `auto_review` | bool | `false` | Auto-review all opened PRs |
| `projects` | list | `[]` | Repo→project mappings |

#### ProjectMapping fields

| Field | Type | Default | Description |
|---|---|---|---|
| `repo` | string | required | GitHub `owner/repo` |
| `project` | string | required | Workforce project name or id |
| `specialist` | string | `null` | Bypass Manager; dispatch this specialist |

## Starting the server

```bash
# Default port 8080, all interfaces
workforce webhook start

# Custom port and host
workforce webhook start --port 9000 --host 127.0.0.1

# Explicit config path
workforce webhook start --config /etc/workforce/webhook.toml
```

Check the running status:

```bash
workforce webhook status
```

Stop the daemon:

```bash
workforce webhook stop
```

The PID is stored in `~/.workforce/webhook.pid`.

## Supported GitHub events

### `issues` — labeled

When an issue receives the `dispatch_label` label (default:
`workforce-dispatch`), a mission is dispatched using the issue's title and body
as the ticket.

**Example flow:**
1. A team member labels issue #42 "Add rate limiting to the API" with
   `workforce-dispatch`.
2. GitHub sends a `POST /webhook` with event type `issues`, action `labeled`.
3. Workforce finds the matching project from `webhook.toml`.
4. `workforce dispatch <project> --file <tmpfile> --ci --background` runs.
5. The specialist picks up the ticket and starts working.

### `pull_request` — opened

When `auto_review = true` and a PR is opened, a Reviewer mission is
dispatched to inspect the changes.

**Example flow:**
1. A developer opens PR #7 "feat: add OAuth2 login".
2. GitHub sends `pull_request` / `opened`.
3. Workforce dispatches a Reviewer on the PR description.
4. The Reviewer posts feedback (visible in mission output / `workforce missions`).

## Setting up the webhook in GitHub

1. Go to your GitHub repository → **Settings** → **Webhooks** → **Add webhook**.
2. Set **Payload URL** to your server's public URL, e.g. `https://example.com/webhook`.
3. Set **Content type** to `application/json`.
4. Set **Secret** to the same value as `secret` in your `webhook.toml`.
5. Select individual events: choose **Issues** and/or **Pull requests** (or
   "Send me everything" during development).
6. Click **Add webhook**.

GitHub will send a `ping` event on creation; the daemon responds `200 OK`.

## Security

Every request is verified with HMAC-SHA256 before any handler runs:

- GitHub signs the raw request body with your `secret` and sends the digest
  in the `X-Hub-Signature-256: sha256=<hex>` header.
- The daemon rejects requests with a missing or incorrect signature with
  `400 Bad Request` / `401 Unauthorized`.
- The comparison uses `hmac.compare_digest` to prevent timing attacks.

**Best practices:**
- Keep `webhook.toml` readable only by the service user (`chmod 600`).
- Rotate the secret periodically (update GitHub webhook settings and
  `webhook.toml` at the same time).
- Run the daemon behind a reverse proxy (nginx/caddy) with TLS — GitHub
  always sends to HTTPS in production.

## Running in production

A simple systemd unit:

```ini
[Unit]
Description=Workforce webhook daemon
After=network.target

[Service]
Type=simple
User=workforce
ExecStart=/usr/local/bin/workforce webhook start --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Or with Docker:

```dockerfile
FROM python:3.12-slim
RUN pip install 'workforce-ai[webhook]'
COPY webhook.toml /root/.workforce/webhook.toml
CMD ["workforce", "webhook", "start", "--host", "0.0.0.0", "--port", "8080"]
```

## Health check

`GET /health` returns `{"status": "ok"}` with HTTP 200. Use this for load
balancer or uptime monitor probes.
