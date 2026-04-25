"""GitHub webhook event handlers.

Each handler receives the parsed event payload and the WebhookConfig, and
optionally dispatches a Workforce mission by shelling out to
``workforce dispatch``.  Handlers return the mission id on dispatch, or None
if the event was ignored (wrong action, no matching project, etc.).

The dispatch call uses ``--ci --background`` so it:
- writes a machine-readable summary to stdout and exits (--ci)
- returns immediately without blocking the webhook response (--background)

The ticket text is written to a temporary file and passed via ``--file`` so
long issue bodies don't hit shell arg-length limits.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import WebhookConfig, ProjectMapping

logger = logging.getLogger(__name__)


def _run_dispatch(
    mapping: ProjectMapping,
    ticket: str,
    *,
    extra_args: list[str] | None = None,
) -> str | None:
    """Write ticket to a temp file and invoke ``workforce dispatch`` in background.

    Args:
        mapping: The ProjectMapping that determines which project/specialist to use.
        ticket: The ticket text to dispatch.
        extra_args: Additional CLI flags appended after the ticket file argument.

    Returns:
        The mission id string printed to stdout by the dispatch process,
        or None if the command failed or produced no output.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="wf-webhook-", delete=False
    ) as tf:
        tf.write(ticket)
        ticket_path = Path(tf.name)

    try:
        argv = [
            sys.executable, "-m", "workforce",
            "dispatch", mapping.project,
            "--file", str(ticket_path),
            "--ci",
            "--background",
        ]
        if mapping.specialist:
            argv += ["--specialist", mapping.specialist]
        if extra_args:
            argv.extend(extra_args)

        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode not in (0, 1, 2):
            logger.error(
                "workforce dispatch exited %d: %s",
                result.returncode,
                result.stderr[:500],
            )
            return None

        # The --ci mode prints "mission_id=<id>" to stdout.
        for line in result.stdout.splitlines():
            if line.startswith("mission_id="):
                return line.split("=", 1)[1].strip()

        # Fallback: return the first non-empty stdout line.
        for line in result.stdout.splitlines():
            if line.strip():
                return line.strip()

        return None
    except subprocess.TimeoutExpired:
        logger.error("workforce dispatch timed out")
        return None
    except OSError as e:
        logger.error("could not run workforce dispatch: %s", e)
        return None
    finally:
        ticket_path.unlink(missing_ok=True)


async def handle_issues(event: dict, config: WebhookConfig) -> str | None:
    """Handle a ``issues`` webhook event.

    Dispatches a mission when the issue is labeled with ``config.dispatch_label``.
    The ticket text is composed from the issue title and body.

    Args:
        event: Parsed JSON payload from GitHub.
        config: The loaded WebhookConfig.

    Returns:
        The dispatched mission id, or None if the event was ignored.
    """
    action = event.get("action")
    if action != "labeled":
        return None

    label_name = (event.get("label") or {}).get("name", "")
    if label_name != config.dispatch_label:
        return None

    issue = event.get("issue") or {}
    repo_full_name = (event.get("repository") or {}).get("full_name", "")

    mapping = config.find_project(repo_full_name)
    if mapping is None:
        logger.info("no project mapping for repo %r — ignoring", repo_full_name)
        return None

    title = issue.get("title", "").strip()
    body = (issue.get("body") or "").strip()
    issue_number = issue.get("number", "?")
    issue_url = issue.get("html_url", "")

    ticket_lines = [f"Issue #{issue_number}: {title}"]
    if issue_url:
        ticket_lines.append(f"URL: {issue_url}")
    if body:
        ticket_lines.append("")
        ticket_lines.append(body)
    ticket = "\n".join(ticket_lines)

    logger.info(
        "dispatching issue #%s from %r on project %r",
        issue_number, repo_full_name, mapping.project,
    )
    return _run_dispatch(mapping, ticket)


async def handle_pull_request(event: dict, config: WebhookConfig) -> str | None:
    """Handle a ``pull_request`` webhook event.

    Dispatches a reviewer mission when a PR is opened and ``config.auto_review``
    is True.

    Args:
        event: Parsed JSON payload from GitHub.
        config: The loaded WebhookConfig.

    Returns:
        The dispatched mission id, or None if the event was ignored.
    """
    if not config.auto_review:
        return None

    action = event.get("action")
    if action != "opened":
        return None

    pr = event.get("pull_request") or {}
    repo_full_name = (event.get("repository") or {}).get("full_name", "")

    mapping = config.find_project(repo_full_name)
    if mapping is None:
        logger.info("no project mapping for repo %r — ignoring", repo_full_name)
        return None

    pr_number = pr.get("number", "?")
    pr_title = pr.get("title", "").strip()
    pr_url = pr.get("html_url", "")
    pr_body = (pr.get("body") or "").strip()
    base_branch = (pr.get("base") or {}).get("ref", "main")
    head_branch = (pr.get("head") or {}).get("ref", "")

    ticket_lines = [
        f"Review pull request #{pr_number}: {pr_title}",
        f"URL: {pr_url}",
        f"Base branch: {base_branch}",
    ]
    if head_branch:
        ticket_lines.append(f"Head branch: {head_branch}")
    if pr_body:
        ticket_lines.append("")
        ticket_lines.append(pr_body)
    ticket = "\n".join(ticket_lines)

    logger.info(
        "dispatching PR review for #%s from %r on project %r",
        pr_number, repo_full_name, mapping.project,
    )
    return _run_dispatch(mapping, ticket, extra_args=["--review"])
