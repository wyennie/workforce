"""FastAPI webhook server.

Receives GitHub webhook POST requests, verifies the HMAC-SHA256 signature,
and dispatches Workforce missions in background tasks.

Usage (via CLI)::

    workforce webhook start --port 8080

Or directly::

    uvicorn workforce.webhook.server:app
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import WebhookConfig, load_webhook_config
from .handlers import handle_issues, handle_pull_request
from .verify import verify_signature

logger = logging.getLogger(__name__)

# FastAPI is an optional dependency ([webhook] extra). Import lazily so the
# rest of the package is importable even without it installed.
try:
    from fastapi import (
        BackgroundTasks,
        FastAPI,
        Header,
        HTTPException,
        Request,
        status,
    )
    from fastapi.responses import JSONResponse
    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FASTAPI_AVAILABLE = False


def create_app(config_path: Path | None = None) -> Any:
    """Create and return the FastAPI application.

    Args:
        config_path: Optional explicit path to ``webhook.toml``. When None,
            the default resolution order applies (env var → ``~/.workforce/webhook.toml``).

    Returns:
        A configured FastAPI application instance.

    Raises:
        ImportError: If FastAPI / uvicorn are not installed
            (install with ``pip install workforce-ai[webhook]``).
    """
    if not _FASTAPI_AVAILABLE:
        raise ImportError(
            "FastAPI is required for the webhook server. "
            "Install it with: pip install workforce-ai[webhook]"
        )

    app = FastAPI(
        title="Workforce Webhook Daemon",
        description="Receives GitHub webhook events and dispatches Workforce missions.",
        version="1.0.0",
    )

    # Load config at startup so bad configs fail fast.
    _config: WebhookConfig | None = None

    def _get_config() -> WebhookConfig:
        nonlocal _config
        if _config is None:
            _config = load_webhook_config(config_path)
        return _config

    @app.post("/webhook", status_code=status.HTTP_200_OK)
    async def webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        x_hub_signature_256: str | None = Header(default=None),
        x_github_event: str | None = Header(default=None),
    ) -> JSONResponse:
        """Receive a GitHub webhook event.

        Verifies the HMAC-SHA256 signature, parses the event type, and
        dispatches the appropriate handler in a background task so GitHub
        receives a fast 200 OK response.
        """
        payload = await request.body()
        config = _get_config()

        # Reject unsigned requests.
        if x_hub_signature_256 is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="missing X-Hub-Signature-256 header",
            )

        if not verify_signature(payload, x_hub_signature_256, config.secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid signature",
            )

        event_type = x_github_event or "unknown"
        try:
            event: dict[str, Any] = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid JSON payload",
            ) from exc

        logger.info("received %r event", event_type)

        if event_type == "issues":
            background_tasks.add_task(_dispatch_issues, event, config)
        elif event_type == "pull_request":
            background_tasks.add_task(_dispatch_pull_request, event, config)
        else:
            logger.debug("no handler for event type %r — ignoring", event_type)

        return JSONResponse({"ok": True, "event": event_type})

    @app.get("/health")
    async def health() -> JSONResponse:
        """Health check endpoint."""
        return JSONResponse({"status": "ok"})

    return app


async def _dispatch_issues(event: dict[str, Any], config: WebhookConfig) -> None:
    """Background task wrapper for handle_issues."""
    try:
        mission_id = await handle_issues(event, config)
        if mission_id:
            logger.info("dispatched issues mission: %s", mission_id)
    except Exception:
        logger.exception("error in issues handler")


async def _dispatch_pull_request(event: dict[str, Any], config: WebhookConfig) -> None:
    """Background task wrapper for handle_pull_request."""
    try:
        mission_id = await handle_pull_request(event, config)
        if mission_id:
            logger.info("dispatched pull_request mission: %s", mission_id)
    except Exception:
        logger.exception("error in pull_request handler")


# Module-level app instance for ``uvicorn workforce.webhook.server:app``.
# Uses the default config resolution (env var / ~/.workforce/webhook.toml).
app = create_app() if _FASTAPI_AVAILABLE else None
