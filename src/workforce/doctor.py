"""Pre-flight environment checks.

Run before anything else to verify Workforce can do its job. Failures abort;
warnings are surfaced but do not abort.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum

from workforce import paths


class Status(StrEnum):
    """Result severity for a single pre-flight check."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class Check:
    """Outcome of one pre-flight check.

    Attributes:
        name: Human-readable check name (e.g. ``"python"``, ``"git"``).
        status: Severity of the outcome.
        detail: One-line explanation or version string shown in the report.
    """

    name: str
    status: Status
    detail: str


MIN_PYTHON = (3, 11)


def check_python() -> Check:
    """Verify the interpreter version meets the minimum requirement."""
    v = sys.version_info
    if (v.major, v.minor) < MIN_PYTHON:
        return Check(
            "python",
            Status.FAIL,
            f"Python {v.major}.{v.minor} found; need >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
        )
    return Check("python", Status.OK, f"{v.major}.{v.minor}.{v.micro}")


def check_sdk() -> Check:
    """Verify that the ``claude_agent_sdk`` package is importable."""
    try:
        importlib.import_module("claude_agent_sdk")
    except ImportError as e:
        return Check("claude-agent-sdk", Status.FAIL, f"import failed: {e}")
    return Check("claude-agent-sdk", Status.OK, "importable")


def check_claude_cli() -> Check:
    """The SDK shells out to the `claude` binary; it must be on PATH."""
    binary = shutil.which("claude")
    if binary is None:
        return Check(
            "claude CLI",
            Status.FAIL,
            "binary not found on PATH (install: https://docs.claude.com/claude-code)",
        )
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return Check("claude CLI", Status.FAIL, f"failed to invoke: {e}")
    if result.returncode != 0:
        return Check("claude CLI", Status.FAIL, f"exit {result.returncode}: {result.stderr.strip()}")
    return Check("claude CLI", Status.OK, f"{binary} ({result.stdout.strip()})")


def check_git() -> Check:
    """Verify that ``git`` is installed and callable."""
    binary = shutil.which("git")
    if binary is None:
        return Check("git", Status.FAIL, "binary not found on PATH")
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return Check("git", Status.FAIL, f"failed to invoke: {e}")
    if result.returncode != 0:
        return Check("git", Status.FAIL, f"exit {result.returncode}")
    return Check("git", Status.OK, result.stdout.strip())


def check_auth() -> Check:
    """Either ANTHROPIC_API_KEY is set, or `claude` is logged in.

    We can't introspect `claude`'s session state cheaply, so absence of the env
    var becomes a warning, not a failure. The SDK will surface a real auth
    error on the first request if neither path works.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return Check("auth", Status.OK, "ANTHROPIC_API_KEY is set")
    return Check(
        "auth",
        Status.WARN,
        "ANTHROPIC_API_KEY not set; relying on `claude` CLI session",
    )


def check_home() -> Check:
    """WORKFORCE_HOME must be creatable and writable."""
    h = paths.home()
    try:
        paths.ensure_layout()
    except OSError as e:
        return Check("workforce home", Status.FAIL, f"cannot create {h}: {e}")
    if not os.access(h, os.W_OK):
        return Check("workforce home", Status.FAIL, f"{h} is not writable")
    return Check("workforce home", Status.OK, str(h))


def run_all() -> list[Check]:
    """Run every pre-flight check and return the results in order."""
    return [
        check_python(),
        check_sdk(),
        check_claude_cli(),
        check_git(),
        check_auth(),
        check_home(),
    ]


def worst(checks: list[Check]) -> Status:
    """Return the most severe status across a list of checks.

    ``FAIL`` beats ``WARN`` beats ``OK``.
    """
    if any(c.status is Status.FAIL for c in checks):
        return Status.FAIL
    if any(c.status is Status.WARN for c in checks):
        return Status.WARN
    return Status.OK
