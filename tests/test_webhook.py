"""Tests for the webhook package: signature verification and event handlers."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from workforce.webhook.config import ProjectMapping, WebhookConfig, load_webhook_config
from workforce.webhook.handlers import handle_issues, handle_pull_request
from workforce.webhook.verify import verify_signature

# ---------------------------------------------------------------------------
# Signature verification
# ---------------------------------------------------------------------------


def _make_signature(payload: bytes, secret: str) -> str:
    """Compute the expected HMAC-SHA256 signature for a payload."""
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class TestVerifySignature:
    def test_valid_signature(self) -> None:
        payload = b'{"action": "labeled"}'
        secret = "super-secret"
        sig = _make_signature(payload, secret)
        assert verify_signature(payload, sig, secret) is True

    def test_invalid_signature(self) -> None:
        payload = b'{"action": "labeled"}'
        secret = "super-secret"
        wrong_sig = _make_signature(payload, "wrong-secret")
        assert verify_signature(payload, wrong_sig, secret) is False

    def test_tampered_payload(self) -> None:
        payload = b'{"action": "labeled"}'
        secret = "super-secret"
        sig = _make_signature(payload, secret)
        tampered = b'{"action": "unlabeled"}'
        assert verify_signature(tampered, sig, secret) is False

    def test_missing_prefix(self) -> None:
        payload = b'{"action": "labeled"}'
        secret = "super-secret"
        raw_hex = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        # Signature without "sha256=" prefix
        assert verify_signature(payload, raw_hex, secret) is False

    def test_empty_payload(self) -> None:
        payload = b""
        secret = "s3cr3t"
        sig = _make_signature(payload, secret)
        assert verify_signature(payload, sig, secret) is True

    def test_wrong_prefix(self) -> None:
        payload = b"hello"
        secret = "s3cr3t"
        # sha1= prefix instead of sha256=
        sig = "sha1=" + hmac.new(secret.encode(), payload, hashlib.sha1).hexdigest()
        assert verify_signature(payload, sig, secret) is False


# ---------------------------------------------------------------------------
# handle_issues
# ---------------------------------------------------------------------------


def _issues_config(
    dispatch_label: str = "workforce-dispatch",
    repo: str = "acme/backend",
    project: str = "backend",
    specialist: str | None = None,
) -> WebhookConfig:
    return WebhookConfig(
        secret="secret",
        dispatch_label=dispatch_label,
        projects=[
            ProjectMapping(repo=repo, project=project, specialist=specialist)
        ],
    )


def _make_issue_event(
    action: str = "labeled",
    label: str = "workforce-dispatch",
    repo: str = "acme/backend",
    title: str = "Fix the bug",
    body: str = "Something is broken.",
    number: int = 42,
) -> dict[str, Any]:
    return {
        "action": action,
        "label": {"name": label},
        "repository": {"full_name": repo},
        "issue": {
            "number": number,
            "title": title,
            "body": body,
            "html_url": f"https://github.com/{repo}/issues/{number}",
        },
    }


class TestHandleIssues:
    @pytest.mark.asyncio
    async def test_dispatches_on_correct_label(self) -> None:
        event = _make_issue_event()
        config = _issues_config()

        with patch(
            "workforce.webhook.handlers._run_dispatch", return_value="mission-abc123"
        ) as mock_dispatch:
            result = await handle_issues(event, config)

        assert result == "mission-abc123"
        mock_dispatch.assert_called_once()
        # Verify the mapping and ticket content passed to _run_dispatch.
        call_args = mock_dispatch.call_args
        mapping = call_args.args[0]
        ticket = call_args.args[1]
        assert mapping.project == "backend"
        assert "Fix the bug" in ticket
        assert "Something is broken." in ticket
        assert "#42" in ticket

    @pytest.mark.asyncio
    async def test_ignores_wrong_label(self) -> None:
        event = _make_issue_event(label="other-label")
        config = _issues_config(dispatch_label="workforce-dispatch")

        with patch("workforce.webhook.handlers._run_dispatch") as mock_dispatch:
            result = await handle_issues(event, config)

        assert result is None
        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_labeled_action(self) -> None:
        event = _make_issue_event(action="closed")
        config = _issues_config()

        with patch("workforce.webhook.handlers._run_dispatch") as mock_dispatch:
            result = await handle_issues(event, config)

        assert result is None
        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_unknown_repo(self) -> None:
        event = _make_issue_event(repo="other/repo")
        config = _issues_config(repo="acme/backend")

        with patch("workforce.webhook.handlers._run_dispatch") as mock_dispatch:
            result = await handle_issues(event, config)

        assert result is None
        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_passes_specialist_to_dispatch(self) -> None:
        event = _make_issue_event()
        config = _issues_config(specialist="senior-engineer")

        with patch(
            "workforce.webhook.handlers._run_dispatch", return_value="m-001"
        ) as mock_dispatch:
            await handle_issues(event, config)

        mapping = mock_dispatch.call_args.args[0]
        assert mapping.specialist == "senior-engineer"

    @pytest.mark.asyncio
    async def test_returns_none_when_dispatch_fails(self) -> None:
        event = _make_issue_event()
        config = _issues_config()

        with patch("workforce.webhook.handlers._run_dispatch", return_value=None):
            result = await handle_issues(event, config)

        assert result is None


# ---------------------------------------------------------------------------
# handle_pull_request
# ---------------------------------------------------------------------------


def _make_pr_event(
    action: str = "opened",
    repo: str = "acme/backend",
    number: int = 7,
    title: str = "Add feature X",
    body: str = "Implements X.",
) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {
            "number": number,
            "title": title,
            "body": body,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "base": {"ref": "main"},
            "head": {"ref": "feature/x"},
        },
    }


class TestHandlePullRequest:
    @pytest.mark.asyncio
    async def test_dispatches_when_auto_review_enabled(self) -> None:
        event = _make_pr_event()
        config = WebhookConfig(
            secret="s",
            auto_review=True,
            projects=[ProjectMapping(repo="acme/backend", project="backend")],
        )

        with patch(
            "workforce.webhook.handlers._run_dispatch", return_value="mission-pr-1"
        ) as mock_dispatch:
            result = await handle_pull_request(event, config)

        assert result == "mission-pr-1"
        mock_dispatch.assert_called_once()
        ticket = mock_dispatch.call_args.args[1]
        assert "Add feature X" in ticket
        assert "#7" in ticket

    @pytest.mark.asyncio
    async def test_ignores_when_auto_review_disabled(self) -> None:
        event = _make_pr_event()
        config = WebhookConfig(
            secret="s",
            auto_review=False,
            projects=[ProjectMapping(repo="acme/backend", project="backend")],
        )

        with patch("workforce.webhook.handlers._run_dispatch") as mock_dispatch:
            result = await handle_pull_request(event, config)

        assert result is None
        mock_dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_opened_action(self) -> None:
        event = _make_pr_event(action="closed")
        config = WebhookConfig(
            secret="s",
            auto_review=True,
            projects=[ProjectMapping(repo="acme/backend", project="backend")],
        )

        with patch("workforce.webhook.handlers._run_dispatch") as mock_dispatch:
            result = await handle_pull_request(event, config)

        assert result is None
        mock_dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# load_webhook_config
# ---------------------------------------------------------------------------


class TestLoadWebhookConfig:
    def test_load_from_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "webhook.toml"
        cfg.write_text(
            'secret = "abc123"\n'
            'dispatch_label = "wf"\n'
            "\n"
            "[[projects]]\n"
            'repo = "owner/repo"\n'
            'project = "myproject"\n'
        )
        loaded = load_webhook_config(cfg)
        assert loaded.secret == "abc123"
        assert loaded.dispatch_label == "wf"
        assert len(loaded.projects) == 1
        assert loaded.projects[0].repo == "owner/repo"

    def test_load_from_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = tmp_path / "webhook.toml"
        cfg.write_text('secret = "env-secret"\n')
        monkeypatch.setenv("WORKFORCE_WEBHOOK_CONFIG", str(cfg))
        loaded = load_webhook_config()
        assert loaded.secret == "env-secret"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_webhook_config(tmp_path / "nonexistent.toml")
