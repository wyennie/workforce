"""WebhookConfig model and loader.

The webhook configuration is a TOML file at ``~/.workforce/webhook.toml`` by
default. Override with the ``WORKFORCE_WEBHOOK_CONFIG`` environment variable.

Example ``webhook.toml``::

    secret = "my-github-webhook-secret"
    dispatch_label = "workforce-dispatch"
    auto_review = false

    [[projects]]
    repo = "acme/backend"
    project = "backend"
    specialist = "senior-engineer"

    [[projects]]
    repo = "acme/frontend"
    project = "frontend"
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel

from workforce import paths


class ProjectMapping(BaseModel):
    """Maps a GitHub repo to a Workforce project."""

    repo: str
    """GitHub repo in ``owner/repo`` form, e.g. ``acme/backend``."""

    project: str
    """Workforce project name or id to dispatch into."""

    specialist: str | None = None
    """If set, bypass the Manager and dispatch this specialist directly."""


class WebhookConfig(BaseModel):
    """Top-level webhook daemon configuration."""

    secret: str
    """GitHub webhook secret used to verify HMAC-SHA256 signatures."""

    dispatch_label: str = "workforce-dispatch"
    """Issue label that triggers an automatic dispatch. Default: ``workforce-dispatch``."""

    auto_review: bool = False
    """If True, open/reopened pull requests are automatically dispatched for review."""

    projects: list[ProjectMapping] = []
    """Mapping from GitHub repos to Workforce projects."""

    def find_project(self, repo: str) -> ProjectMapping | None:
        """Return the first ProjectMapping whose ``repo`` matches, or None."""
        for m in self.projects:
            if m.repo.lower() == repo.lower():
                return m
        return None


def load_webhook_config(path: Path | None = None) -> WebhookConfig:
    """Load WebhookConfig from TOML.

    Reads from ``path`` if given, otherwise checks the
    ``WORKFORCE_WEBHOOK_CONFIG`` env var, and finally falls back to
    ``~/.workforce/webhook.toml``.

    Args:
        path: Explicit path to the config file.

    Returns:
        A validated WebhookConfig instance.

    Raises:
        FileNotFoundError: If the resolved config file does not exist.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
        pydantic.ValidationError: If the config doesn't match the schema.
    """
    if path is None:
        env_path = os.environ.get("WORKFORCE_WEBHOOK_CONFIG")
        if env_path:
            path = Path(env_path)
        else:
            path = paths.home() / "webhook.toml"

    with path.open("rb") as f:
        data = tomllib.load(f)

    return WebhookConfig.model_validate(data)
