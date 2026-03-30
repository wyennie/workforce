"""Path-ownership enforcement via the Claude Agent SDK's `can_use_tool` hook.

Each parallel sub-mission declares an `owns_paths` lane (with optional
`excludes_paths` carve-outs) at plan time. This module turns that declaration
into a runtime gate: file-writing tool calls (Edit / Write / MultiEdit) whose
target path falls outside the lane are denied with a structured message the
specialist can act on. Reads are unrestricted — the contract is "don't write
outside your lane," not "don't see outside your lane."

The Manager already validates non-overlapping lanes at plan time
(`manager._check_parallel_overlap`); this is the runtime side of the contract.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from workforce.globmatch import glob_to_regex

# Type alias matching the SDK's CanUseTool signature.
CanUseTool = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]


# Tools we gate. Read/Glob/Grep/Bash are not gated: the contract is on writes.
# Bash escape (e.g. `echo > file`) is out of scope here; restrict it via the
# specialist's allowed_tools when running collaborative parallel missions.
_WRITE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# Tool input keys that hold the target path, by tool name.
_PATH_KEYS: dict[str, str] = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def _path_in_lane(
    rel_path: str,
    owns_regexes: list[re.Pattern[str]],
    excludes_regexes: list[re.Pattern[str]],
) -> bool:
    """True if `rel_path` matches any owns pattern and no excludes pattern."""
    if not any(rx.match(rel_path) for rx in owns_regexes):
        return False
    return not any(rx.match(rel_path) for rx in excludes_regexes)


def _normalize_for_match(p: Path) -> str:
    """POSIX-style relative path string for glob matching."""
    return p.as_posix()


def make_path_owner_callback(
    *,
    cwd: Path,
    owns_paths: list[str],
    excludes_paths: list[str],
) -> CanUseTool:
    """Build a `can_use_tool` callback that enforces a write-path lane.

    The callback denies any Edit/Write/MultiEdit/NotebookEdit whose target
    file_path resolves outside the lane defined by `owns_paths` (with
    `excludes_paths` carved out). All other tools are allowed unchanged.

    `cwd` is the mission's working directory (worktree for repo missions, the
    project dir for workspace missions). Paths supplied by the agent are
    resolved against `cwd` and then matched against the lane.

    Empty `owns_paths` is treated as "no constraint configured" — the callback
    is effectively a no-op. Callers who want strict denial should not call
    this builder when the Manager didn't declare a lane.
    """
    cwd_resolved = cwd.resolve()
    owns_regexes = [glob_to_regex(p) for p in owns_paths]
    excludes_regexes = [glob_to_regex(p) for p in excludes_paths]

    async def callback(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name not in _WRITE_TOOLS:
            return PermissionResultAllow()
        if not owns_regexes:
            # No lane declared → don't enforce. Audit still runs post-hoc.
            return PermissionResultAllow()

        path_key = _PATH_KEYS[tool_name]
        raw = tool_input.get(path_key)
        if not isinstance(raw, str) or not raw:
            # Malformed call — let the SDK/tool produce its own error.
            return PermissionResultAllow()

        target = Path(raw)
        if not target.is_absolute():
            target = cwd_resolved / target
        try:
            target_resolved = target.resolve()
        except OSError:
            return PermissionResultDeny(
                message=(
                    f"can't resolve path {raw!r}: stay within "
                    f"{cwd_resolved.as_posix()} and your declared lane."
                ),
            )

        # Disallow any write outside the cwd entirely (no /etc/, no ../, etc.).
        try:
            rel = target_resolved.relative_to(cwd_resolved)
        except ValueError:
            return PermissionResultDeny(
                message=(
                    f"path {raw!r} is outside this mission's working directory "
                    f"({cwd_resolved.as_posix()}). Stay inside cwd."
                ),
            )

        rel_str = _normalize_for_match(rel)
        if _path_in_lane(rel_str, owns_regexes, excludes_regexes):
            return PermissionResultAllow()

        owns_summary = ", ".join(owns_paths) if owns_paths else "(none)"
        excludes_summary = (
            f"; excludes: {', '.join(excludes_paths)}" if excludes_paths else ""
        )
        return PermissionResultDeny(
            message=(
                f"path {rel_str!r} is outside this task's lane "
                f"(owns: {owns_summary}{excludes_summary}). "
                "Stay in your lane, or finish and leave a note for the next "
                "mission to pick up the out-of-lane work."
            ),
        )

    return callback
