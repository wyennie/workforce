"""Tests for path-ownership enforcement (workforce/permissions.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from workforce.globmatch import glob_to_regex
from workforce.permissions import make_path_owner_callback

# ----- glob → regex ---------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        # Single-component globs
        ("*.py", "foo.py", True),
        ("*.py", "foo.txt", False),
        ("*.py", "sub/foo.py", False),  # * doesn't cross /
        # Multi-component globs
        ("app/*.py", "app/main.py", True),
        ("app/*.py", "app/sub/main.py", False),
        ("app/api/*.py", "app/api/v1.py", True),
        ("app/api/*.py", "app/api/v1/handler.py", False),
        # ** at start
        ("**/conftest.py", "conftest.py", True),
        ("**/conftest.py", "tests/conftest.py", True),
        ("**/conftest.py", "a/b/c/conftest.py", True),
        ("**/conftest.py", "conftest.py.bak", False),
        # ** at end
        ("app/**", "app/main.py", True),
        ("app/**", "app/sub/main.py", True),
        ("app/**", "app", False),  # no trailing slash → no zero-component match
        ("app/**", "other/main.py", False),
        # ** in middle
        ("app/**/handler.py", "app/handler.py", True),
        ("app/**/handler.py", "app/x/handler.py", True),
        ("app/**/handler.py", "app/x/y/handler.py", True),
        ("app/**/handler.py", "app/handler.py.bak", False),
        # ? wildcard
        ("file?.py", "file1.py", True),
        ("file?.py", "file12.py", False),
        ("file?.py", "file/.py", False),  # ? doesn't cross /
        # Char classes
        ("[ab]_test.py", "a_test.py", True),
        ("[ab]_test.py", "c_test.py", False),
        # Literal special chars
        ("a.b.c", "a.b.c", True),
        ("a.b.c", "axbxc", False),
    ],
)
def test_glob_to_regex(pattern: str, path: str, expected: bool) -> None:
    rx = glob_to_regex(pattern)
    assert bool(rx.match(path)) is expected, f"{pattern} vs {path}"


# ----- callback fixtures ----------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A directory we treat as the mission cwd."""
    d = tmp_path / "ws"
    d.mkdir()
    (d / "app").mkdir()
    (d / "app" / "api").mkdir()
    (d / "tests").mkdir()
    return d


def _ctx() -> ToolPermissionContext:
    return ToolPermissionContext(
        signal=None,
        suggestions=[],
        tool_use_id="t1",
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ----- callback behavior ----------------------------------------------------


def test_non_write_tools_always_allowed(workspace: Path) -> None:
    """Read/Glob/Grep/Bash are not gated — only write tools."""
    cb = make_path_owner_callback(
        cwd=workspace, owns_paths=["app/api/**"], excludes_paths=[],
    )
    for tool in ("Read", "Glob", "Grep", "Bash", "Task", "WebFetch"):
        result = _run(cb(tool, {"file_path": "/etc/passwd"}, _ctx()))
        assert isinstance(result, PermissionResultAllow), tool


def test_in_lane_write_allowed(workspace: Path) -> None:
    cb = make_path_owner_callback(
        cwd=workspace, owns_paths=["app/api/**"], excludes_paths=[],
    )
    target = workspace / "app" / "api" / "v1.py"
    result = _run(cb("Write", {"file_path": str(target), "content": "x"}, _ctx()))
    assert isinstance(result, PermissionResultAllow)


def test_in_lane_relative_path_allowed(workspace: Path) -> None:
    """Agent passes a relative path; it's resolved against cwd before matching."""
    cb = make_path_owner_callback(
        cwd=workspace, owns_paths=["app/**"], excludes_paths=[],
    )
    result = _run(cb("Edit", {"file_path": "app/main.py"}, _ctx()))
    assert isinstance(result, PermissionResultAllow)


def test_out_of_lane_write_denied(workspace: Path) -> None:
    cb = make_path_owner_callback(
        cwd=workspace, owns_paths=["app/api/**"], excludes_paths=[],
    )
    target = workspace / "tests" / "test_x.py"
    result = _run(cb("Write", {"file_path": str(target), "content": "x"}, _ctx()))
    assert isinstance(result, PermissionResultDeny)
    assert "outside this task's lane" in result.message
    assert "app/api/**" in result.message  # echoed for the agent


def test_excludes_carve_out(workspace: Path) -> None:
    """`excludes_paths` removes a sub-pattern from the lane."""
    cb = make_path_owner_callback(
        cwd=workspace,
        owns_paths=["app/**"],
        excludes_paths=["app/api/**"],
    )
    in_lane = workspace / "app" / "main.py"
    out_of_lane = workspace / "app" / "api" / "v1.py"
    assert isinstance(_run(cb("Edit", {"file_path": str(in_lane)}, _ctx())), PermissionResultAllow)
    deny = _run(cb("Edit", {"file_path": str(out_of_lane)}, _ctx()))
    assert isinstance(deny, PermissionResultDeny)
    assert "excludes" in deny.message


def test_path_outside_cwd_denied(workspace: Path) -> None:
    """Even an `owns_paths=['**']` lane can't authorize writes outside cwd."""
    cb = make_path_owner_callback(
        cwd=workspace, owns_paths=["**"], excludes_paths=[],
    )
    deny = _run(cb("Write", {"file_path": "/etc/passwd"}, _ctx()))
    assert isinstance(deny, PermissionResultDeny)
    assert "outside this mission's working directory" in deny.message


def test_dotdot_escape_denied(workspace: Path) -> None:
    """A `../` escape resolves outside cwd → deny."""
    cb = make_path_owner_callback(
        cwd=workspace, owns_paths=["**"], excludes_paths=[],
    )
    target = workspace / ".." / "outside.py"
    deny = _run(cb("Write", {"file_path": str(target)}, _ctx()))
    assert isinstance(deny, PermissionResultDeny)


def test_empty_owns_is_no_op(workspace: Path) -> None:
    """No declared lane → callback allows all writes (audit still runs post-hoc)."""
    cb = make_path_owner_callback(
        cwd=workspace, owns_paths=[], excludes_paths=[],
    )
    target = workspace / "tests" / "anything.py"
    result = _run(cb("Write", {"file_path": str(target)}, _ctx()))
    assert isinstance(result, PermissionResultAllow)


def test_multiedit_and_notebookedit_gated(workspace: Path) -> None:
    cb = make_path_owner_callback(
        cwd=workspace, owns_paths=["app/api/**"], excludes_paths=[],
    )
    out = workspace / "tests" / "x.py"
    deny_multi = _run(cb("MultiEdit", {"file_path": str(out)}, _ctx()))
    assert isinstance(deny_multi, PermissionResultDeny)
    deny_nb = _run(cb("NotebookEdit", {"notebook_path": str(out)}, _ctx()))
    assert isinstance(deny_nb, PermissionResultDeny)


def test_malformed_input_allowed(workspace: Path) -> None:
    """Missing/empty file_path → don't try to enforce; let the SDK error."""
    cb = make_path_owner_callback(
        cwd=workspace, owns_paths=["app/**"], excludes_paths=[],
    )
    result = _run(cb("Write", {}, _ctx()))
    assert isinstance(result, PermissionResultAllow)
    result = _run(cb("Write", {"file_path": ""}, _ctx()))
    assert isinstance(result, PermissionResultAllow)
