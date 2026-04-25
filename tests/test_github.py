"""Tests for workforce.github — fetch_issue, fetch_pr, create_pr."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from workforce import github as github_mod
from workforce.github import _parse_issue_url, _parse_pr_url, create_pr, fetch_issue, fetch_pr


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


class TestParseIssueUrl:
    def test_full_https_url(self) -> None:
        owner, repo, num = _parse_issue_url("https://github.com/acme/myrepo/issues/42")
        assert owner == "acme"
        assert repo == "myrepo"
        assert num == 42

    def test_http_url(self) -> None:
        owner, repo, num = _parse_issue_url("http://github.com/acme/myrepo/issues/7")
        assert owner == "acme"
        assert repo == "myrepo"
        assert num == 7

    def test_shorthand_owner_repo_hash(self) -> None:
        owner, repo, num = _parse_issue_url("acme/myrepo#99")
        assert owner == "acme"
        assert repo == "myrepo"
        assert num == 99

    def test_invalid_url_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse GitHub issue URL"):
            _parse_issue_url("not-a-url")


class TestParsePrUrl:
    def test_full_https_url(self) -> None:
        owner, repo, num = _parse_pr_url("https://github.com/acme/myrepo/pull/101")
        assert owner == "acme"
        assert repo == "myrepo"
        assert num == 101

    def test_shorthand(self) -> None:
        owner, repo, num = _parse_pr_url("acme/myrepo#55")
        assert owner == "acme"
        assert repo == "myrepo"
        assert num == 55

    def test_invalid_url_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse GitHub PR URL"):
            _parse_pr_url("bad-input")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_result(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    """Build a CompletedProcess-like mock."""
    r = MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


# ---------------------------------------------------------------------------
# fetch_issue
# ---------------------------------------------------------------------------


class TestFetchIssue:
    def _issue_payload(
        self,
        title: str = "Test issue",
        body: str = "Issue body",
        comments: list[dict] | None = None,
    ) -> str:
        return json.dumps(
            {"title": title, "body": body, "comments": comments or []}
        )

    def test_basic_no_comments(self) -> None:
        payload = self._issue_payload(title="Fix the bug", body="Details here")
        with patch("subprocess.run", return_value=_make_run_result(payload)):
            result = fetch_issue("https://github.com/acme/repo/issues/1")
        assert result.startswith("## Fix the bug")
        assert "Details here" in result
        assert "## Context" not in result

    def test_with_comments(self) -> None:
        comments = [
            {"body": "First comment"},
            {"body": "Second comment"},
            {"body": "Third comment"},
            {"body": "Fourth comment — should be excluded"},
        ]
        payload = self._issue_payload(title="T", body="B", comments=comments)
        with patch("subprocess.run", return_value=_make_run_result(payload)):
            result = fetch_issue("https://github.com/acme/repo/issues/2")
        assert "## Context" in result
        assert "First comment" in result
        assert "Second comment" in result
        assert "Third comment" in result
        assert "Fourth comment" not in result

    def test_comment_truncated(self) -> None:
        long_body = "x" * 600
        comments = [{"body": long_body}]
        payload = self._issue_payload(comments=comments)
        with patch("subprocess.run", return_value=_make_run_result(payload)):
            result = fetch_issue("https://github.com/acme/repo/issues/3")
        assert "…" in result
        # Must be shorter than original 600 chars
        context_block = result.split("## Context")[-1]
        assert len(context_block) < 600

    def test_gh_not_installed_raises(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="gh CLI not found"):
                fetch_issue("https://github.com/acme/repo/issues/1")

    def test_gh_nonzero_exit_raises(self) -> None:
        with patch(
            "subprocess.run",
            return_value=_make_run_result("", returncode=1, stderr="repo not found"),
        ):
            with pytest.raises(RuntimeError, match="failed"):
                fetch_issue("https://github.com/acme/repo/issues/1")

    def test_shorthand_url(self) -> None:
        payload = self._issue_payload(title="Short", body="body")
        with patch("subprocess.run", return_value=_make_run_result(payload)) as mock_run:
            fetch_issue("acme/repo#10")
        call_args = mock_run.call_args[0][0]
        assert "10" in call_args
        assert "acme/repo" in call_args


# ---------------------------------------------------------------------------
# fetch_pr
# ---------------------------------------------------------------------------


class TestFetchPr:
    def _pr_payload(
        self,
        title: str = "My PR",
        body: str = "PR description",
        additions: int = 10,
        deletions: int = 5,
        changed_files: int = 3,
    ) -> str:
        return json.dumps(
            {
                "title": title,
                "body": body,
                "additions": additions,
                "deletions": deletions,
                "changedFiles": changed_files,
            }
        )

    def test_basic(self) -> None:
        payload = self._pr_payload(title="Add feature", body="Does stuff", additions=20, deletions=3, changed_files=4)
        with patch("subprocess.run", return_value=_make_run_result(payload)):
            result = fetch_pr("https://github.com/acme/repo/pull/5")
        assert result.startswith("## Add feature")
        assert "Does stuff" in result
        assert "Changed files: 4, +20/-3 lines" in result

    def test_gh_not_installed_raises(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="gh CLI not found"):
                fetch_pr("https://github.com/acme/repo/pull/1")

    def test_gh_nonzero_exit_raises(self) -> None:
        with patch(
            "subprocess.run",
            return_value=_make_run_result("", returncode=1, stderr="auth error"),
        ):
            with pytest.raises(RuntimeError, match="failed"):
                fetch_pr("https://github.com/acme/repo/pull/1")

    def test_shorthand_url(self) -> None:
        payload = self._pr_payload()
        with patch("subprocess.run", return_value=_make_run_result(payload)) as mock_run:
            fetch_pr("acme/repo#20")
        call_args = mock_run.call_args[0][0]
        assert "20" in call_args
        assert "acme/repo" in call_args


# ---------------------------------------------------------------------------
# create_pr
# ---------------------------------------------------------------------------


class TestCreatePr:
    def test_returns_url(self) -> None:
        url = "https://github.com/acme/repo/pull/42\n"
        with patch("subprocess.run", return_value=_make_run_result(url)):
            result = create_pr(
                repo_path="/tmp/repo",
                branch="feature/my-branch",
                title="My PR",
                body="body text",
            )
        assert result == "https://github.com/acme/repo/pull/42"

    def test_draft_flag_included(self) -> None:
        url = "https://github.com/acme/repo/pull/1\n"
        with patch("subprocess.run", return_value=_make_run_result(url)) as mock_run:
            create_pr(
                repo_path="/tmp/repo",
                branch="feat",
                title="title",
                body="body",
                draft=True,
            )
        call_args = mock_run.call_args[0][0]
        assert "--draft" in call_args

    def test_no_draft_flag_when_false(self) -> None:
        url = "https://github.com/acme/repo/pull/1\n"
        with patch("subprocess.run", return_value=_make_run_result(url)) as mock_run:
            create_pr(
                repo_path="/tmp/repo",
                branch="feat",
                title="title",
                body="body",
                draft=False,
            )
        call_args = mock_run.call_args[0][0]
        assert "--draft" not in call_args

    def test_base_branch_included(self) -> None:
        url = "https://github.com/acme/repo/pull/1\n"
        with patch("subprocess.run", return_value=_make_run_result(url)) as mock_run:
            create_pr(
                repo_path="/tmp/repo",
                branch="feat",
                title="title",
                body="body",
                base="develop",
            )
        call_args = mock_run.call_args[0][0]
        assert "--base" in call_args
        base_idx = call_args.index("--base")
        assert call_args[base_idx + 1] == "develop"

    def test_body_truncated_at_65000(self) -> None:
        long_body = "x" * 70_000
        url = "https://github.com/acme/repo/pull/1\n"
        with patch("subprocess.run", return_value=_make_run_result(url)) as mock_run:
            create_pr(
                repo_path="/tmp/repo",
                branch="feat",
                title="title",
                body=long_body,
            )
        call_args = mock_run.call_args[0][0]
        body_idx = call_args.index("--body")
        actual_body = call_args[body_idx + 1]
        assert len(actual_body) == 65_000

    def test_cwd_passed_to_subprocess(self) -> None:
        url = "https://github.com/acme/repo/pull/1\n"
        with patch("subprocess.run", return_value=_make_run_result(url)) as mock_run:
            create_pr(
                repo_path="/my/repo",
                branch="feat",
                title="title",
                body="body",
            )
        assert mock_run.call_args[1]["cwd"] == "/my/repo"

    def test_gh_not_installed_raises(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="gh CLI not found"):
                create_pr(
                    repo_path="/tmp/repo",
                    branch="feat",
                    title="title",
                    body="body",
                )

    def test_gh_nonzero_exit_raises(self) -> None:
        with patch(
            "subprocess.run",
            return_value=_make_run_result("", returncode=1, stderr="remote not found"),
        ):
            with pytest.raises(RuntimeError, match="failed"):
                create_pr(
                    repo_path="/tmp/repo",
                    branch="feat",
                    title="title",
                    body="body",
                )
