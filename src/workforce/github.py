"""GitHub integration helpers for workforce dispatch.

Wraps the ``gh`` CLI to fetch issues/PRs and create pull requests.
All functions raise ``RuntimeError`` when ``gh`` is not installed or returns
a non-zero exit code, so callers can surface the error cleanly.
"""

from __future__ import annotations

import json
import re
import subprocess

# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


def _parse_issue_url(url: str) -> tuple[str, str, int]:
    """Return (owner, repo, number) from a GitHub issue URL or shorthand.

    Accepts:
    - ``https://github.com/owner/repo/issues/123``
    - ``owner/repo#123``
    """
    # Full URL form
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)", url
    )
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    # Shorthand owner/repo#123
    m = re.match(r"([^/]+)/([^#]+)#(\d+)$", url)
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    raise ValueError(
        f"Cannot parse GitHub issue URL: {url!r}. "
        "Use https://github.com/owner/repo/issues/123 or owner/repo#123."
    )


def _parse_pr_url(url: str) -> tuple[str, str, int]:
    """Return (owner, repo, number) from a GitHub PR URL or shorthand.

    Accepts:
    - ``https://github.com/owner/repo/pull/456``
    - ``owner/repo#456``
    """
    # Full URL form
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url
    )
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    # Shorthand owner/repo#123
    m = re.match(r"([^/]+)/([^#]+)#(\d+)$", url)
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    raise ValueError(
        f"Cannot parse GitHub PR URL: {url!r}. "
        "Use https://github.com/owner/repo/pull/456 or owner/repo#456."
    )


# ---------------------------------------------------------------------------
# gh subprocess helper
# ---------------------------------------------------------------------------


def _run_gh(*args: str, cwd: str | None = None) -> str:
    """Run ``gh`` with *args* and return stdout.

    Raises ``RuntimeError`` if ``gh`` is not on PATH or returns non-zero.
    """
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "gh CLI not found. Install it from https://cli.github.com/ "
            "and authenticate with `gh auth login`."
        ) from None

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"`gh {' '.join(args)}` failed (exit {result.returncode}): {detail}\n"
            "If gh is not authenticated, run `gh auth login` first."
        )

    return result.stdout


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_COMMENT_TRUNCATE = 500
_BODY_COMMENT_LIMIT = 3


def fetch_issue(url: str) -> str:
    """Fetch a GitHub issue and return formatted ticket text.

    Parses *url* (full GitHub URL or ``owner/repo#N`` shorthand), calls
    ``gh issue view`` to retrieve title, body, and comments, and composes:

    .. code-block:: text

        ## {title}

        {body}

        ## Context
        {first 3 comment bodies, each truncated to 500 chars}

    Raises ``RuntimeError`` if gh is not installed or returns non-zero.
    """
    owner, repo, number = _parse_issue_url(url)
    raw = _run_gh(
        "issue", "view", str(number),
        "--repo", f"{owner}/{repo}",
        "--json", "title,body,comments",
    )
    data = json.loads(raw)
    title: str = data.get("title", "")
    body: str = data.get("body", "") or ""
    comments: list[dict] = data.get("comments", []) or []

    parts = [f"## {title}", "", body.strip()]

    top_comments = comments[:_BODY_COMMENT_LIMIT]
    if top_comments:
        parts.append("")
        parts.append("## Context")
        for c in top_comments:
            comment_body: str = (c.get("body") or "").strip()
            if len(comment_body) > _COMMENT_TRUNCATE:
                comment_body = comment_body[:_COMMENT_TRUNCATE] + "…"
            if comment_body:
                parts.append(comment_body)

    return "\n".join(parts)


def fetch_pr(url: str) -> str:
    """Fetch a GitHub PR and return a short summary.

    Returns:

    .. code-block:: text

        ## {title}

        {body}

        Changed files: {changedFiles}, +{additions}/-{deletions} lines

    Raises ``RuntimeError`` if gh is not installed or returns non-zero.
    """
    owner, repo, number = _parse_pr_url(url)
    raw = _run_gh(
        "pr", "view", str(number),
        "--repo", f"{owner}/{repo}",
        "--json", "title,body,additions,deletions,changedFiles",
    )
    data = json.loads(raw)
    title: str = data.get("title", "")
    body: str = (data.get("body") or "").strip()
    additions: int = data.get("additions", 0)
    deletions: int = data.get("deletions", 0)
    changed_files: int = data.get("changedFiles", 0)

    parts = [
        f"## {title}",
        "",
        body,
        "",
        f"Changed files: {changed_files}, +{additions}/-{deletions} lines",
    ]
    return "\n".join(parts)


_PR_BODY_LIMIT = 65_000


def create_pr(
    repo_path: str,
    branch: str,
    title: str,
    body: str,
    base: str = "main",
    draft: bool = False,
) -> str:
    """Create a GitHub pull request and return its URL.

    Runs ``gh pr create`` in *repo_path* with the given *title*, *body*,
    *base* branch, and optional *draft* flag.

    Returns the PR URL string from stdout.
    Raises ``RuntimeError`` if gh is not installed or returns non-zero.
    """
    if len(body) > _PR_BODY_LIMIT:
        body = body[:_PR_BODY_LIMIT]

    args = [
        "pr", "create",
        "--title", title,
        "--body", body,
        "--base", base,
        "--head", branch,
    ]
    if draft:
        args.append("--draft")

    url = _run_gh(*args, cwd=repo_path)
    return url.strip()
