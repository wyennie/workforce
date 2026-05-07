"""Shared helpers for the mission-related CLI modules.

Lives here so cli_dispatch, cli_merge, cli_mission_inspect, and cli_cleanup
can pull from one place without circular imports.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from workforce import mission, output, paths
from workforce import project as project_mod
from workforce.mission import MissionMeta, MissionStatus
from workforce.parallel import ParallelMissionMeta, ParallelStatus
from workforce.specialist import RosterStore
from workforce.worktree import WorktreeManager


def _stores() -> tuple[RosterStore, project_mod.ProjectStore, WorktreeManager]:
    """Ensure the on-disk layout and return the three main data-access objects."""
    paths.ensure_layout()
    return RosterStore(), project_mod.ProjectStore(), WorktreeManager()


def _resolve_specialist(
    proj: project_mod.Project,
    roster_store: RosterStore,
    requested: str | None,
) -> str:
    """Pick a specialist from the project's roster; honor --specialist if given."""
    assigned = proj.assigned_specialists
    if not assigned:
        output.die(
            f"no specialists assigned to project {proj.name!r}. "
            f"Run `workforce project assign {proj.name} <specialist>` first."
        )
    if requested is not None:
        if requested not in assigned:
            output.die(
                f"specialist {requested!r} is not assigned to {proj.name}. "
                f"Assigned: {', '.join(assigned)}"
            )
        if not roster_store.exists(requested):
            output.die(f"specialist {requested!r} no longer exists in the roster")
        return requested
    if len(assigned) == 1:
        if not roster_store.exists(assigned[0]):
            output.die(
                f"specialist {assigned[0]!r} is assigned to {proj.name} "
                "but is no longer in the roster"
            )
        return assigned[0]
    output.die(
        f"{proj.name!r} has {len(assigned)} assigned specialists "
        f"({', '.join(assigned)}); pass --specialist to choose one"
    )
    raise AssertionError("unreachable")  # pragma: no cover - die() exits


def _truncate(s: str, n: int) -> str:
    """Truncate *s* to at most *n* characters (appending … when clipped)."""
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _summarize_tool_args(name: str, args: dict[str, Any]) -> str:
    """Best-effort one-line preview of the more interesting argument."""
    for key in ("file_path", "path", "command", "pattern", "url", "query"):
        if key in args:
            return f"{key}={args[key]!r}"
    if args:
        first_key = next(iter(args))
        return f"{first_key}={args[first_key]!r}"
    return ""


def _make_renderer() -> Any:
    """Returns a function suitable for `on_message=`."""

    def render(msg: Any) -> None:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text = block.text.rstrip()
                    if text:
                        output.info(text)
                elif isinstance(block, ToolUseBlock):
                    args_preview = _truncate(_summarize_tool_args(block.name, block.input), 80)
                    output.info(f"[dim]→ {block.name}({args_preview})[/dim]")
                elif isinstance(block, ThinkingBlock):
                    pass  # noisy; skip in live view (still in events.jsonl)
        elif isinstance(msg, UserMessage):
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock) and block.is_error:
                        preview = _truncate(repr(block.content), 120)
                        output.warn(f"  ← tool error: {preview}")
        elif isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                pass  # noise; skip
        elif isinstance(msg, ResultMessage):
            output.info(
                f"[dim]turns={msg.num_turns} duration={msg.duration_ms}ms "
                f"cost=${(msg.total_cost_usd or 0):.4f}[/dim]"
            )

    return render


# Status -> Rich-styled label. Defined once here so every renderer agrees.

_STATUS_STYLES = {
    MissionStatus.RUNNING: "[cyan]running[/cyan]",
    MissionStatus.COMPLETED: "[green]completed[/green]",
    MissionStatus.ERROR: "[red]error[/red]",
    MissionStatus.WALL_TIMEOUT: "[yellow]wall_timeout[/yellow]",
    MissionStatus.INTERRUPTED: "[yellow]interrupted[/yellow]",
    MissionStatus.REVIEW_REJECTED: "[red]review_rejected[/red]",
}

_PARALLEL_STATUS_STYLES = {
    ParallelStatus.PLANNED: "[dim]planned[/dim]",
    ParallelStatus.DISPATCHED: "[yellow]dispatched[/yellow]",
    ParallelStatus.COMPLETED: "[green]completed[/green]",
    ParallelStatus.PARTIAL: "[yellow]partial[/yellow]",
    ParallelStatus.FAILED: "[red]failed[/red]",
    ParallelStatus.CANCELLED: "[dim]cancelled[/dim]",
}

# Pill-style badge labels for use in detail views (mission show, post-dispatch).
_STATUS_BADGES: dict[MissionStatus, str] = {
    MissionStatus.RUNNING: "[bold white on cyan] RUNNING [/bold white on cyan]",
    MissionStatus.COMPLETED: "[bold white on green] DONE [/bold white on green]",
    MissionStatus.ERROR: "[bold white on red] ERROR [/bold white on red]",
    MissionStatus.WALL_TIMEOUT: "[bold black on yellow] TIMEOUT [/bold black on yellow]",
    MissionStatus.INTERRUPTED: "[bold black on yellow] INTERRUPTED [/bold black on yellow]",
    MissionStatus.REVIEW_REJECTED: "[bold white on red] REJECTED [/bold white on red]",
}

_PARALLEL_STATUS_BADGES: dict[ParallelStatus, str] = {
    ParallelStatus.PLANNED: "[dim] PLANNED [/dim]",
    ParallelStatus.DISPATCHED: "[bold black on yellow] RUNNING [/bold black on yellow]",
    ParallelStatus.COMPLETED: "[bold white on green] DONE [/bold white on green]",
    ParallelStatus.PARTIAL: "[bold black on yellow] PARTIAL [/bold black on yellow]",
    ParallelStatus.FAILED: "[bold white on red] FAILED [/bold white on red]",
    ParallelStatus.CANCELLED: "[dim] CANCELLED [/dim]",
}

# Per-category tool call colors for tail/replay rendering.
_TOOL_COLORS: dict[str, str] = {
    # file I/O
    "Read": "blue",
    "Write": "blue",
    "Edit": "blue",
    # search
    "Grep": "cyan",
    "Glob": "cyan",
    # execution
    "Bash": "yellow",
    # web
    "WebFetch": "magenta",
    "WebSearch": "magenta",
    # sub-agents
    "Agent": "green",
}


def _tool_color(name: str) -> str:
    """Return the Rich color name for a given tool, falling back to 'dim'."""
    return _TOOL_COLORS.get(name, "dim")


def _relative_time(iso: str | None) -> str:
    """Convert an ISO-8601 UTC string to a human-readable relative time string."""
    if not iso:
        return "[dim](unknown)[/dim]"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        s = int(delta.total_seconds())
        if s < 60:
            return "just now"
        if s < 3600:
            return f"{s // 60}m ago"
        if s < 86400:
            return f"{s // 3600}h ago"
        if s < 7 * 86400:
            return f"{s // 86400}d ago"
        return dt.strftime("%b %d")
    except ValueError:
        return iso[:16]  # fallback: first 16 chars of ISO string


# ----- mission lookup -------------------------------------------------------


def _load_any_meta(
    project_id: str, mission_id: str
) -> MissionMeta | ParallelMissionMeta | None:
    """Load either a single-mission meta or a parent parallel meta from disk.

    Returns None if the meta.json file is absent or unparseable.
    """
    mp = mission.mission_paths(project_id, mission_id)
    if not mp.meta.is_file():
        return None
    text = mp.meta.read_text()
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if "parent_mission_id" in data:
        try:
            return ParallelMissionMeta.model_validate(data)
        except ValueError:
            return None
    try:
        return MissionMeta.model_validate(data)
    except ValueError:
        return None


def _find_mission(mission_id: str) -> tuple[project_mod.Project, MissionMeta | ParallelMissionMeta]:
    """Locate a mission (single, parent, or sub) across all registered projects.

    Requires meta.json to exist — for in-progress missions that haven't yet
    written meta, use `_find_mission_dir` instead.
    """
    pstore = project_mod.ProjectStore()
    for proj in pstore.list():
        meta = _load_any_meta(proj.id, mission_id)
        if meta is not None:
            return proj, meta
    output.die(f"no mission with id {mission_id!r} found in any project")
    raise AssertionError("unreachable")  # pragma: no cover


def _find_mission_dir(
    mission_id: str,
) -> tuple[project_mod.Project, mission.MissionPaths] | None:
    """Locate a mission's on-disk directory by id, without requiring meta.json.

    Returns (project, mission_paths) if a `<project>/missions/<mission_id>/`
    directory exists. Used by `mission tail` so it can attach to an in-progress
    mission whose meta.json hasn't been written yet.
    """
    pstore = project_mod.ProjectStore()
    for proj in pstore.list():
        mp = mission.mission_paths(proj.id, mission_id)
        if mp.root.is_dir():
            return proj, mp
    return None


def _list_project_missions(
    project_id: str,
) -> list[MissionMeta | ParallelMissionMeta]:
    """Return all parseable mission metas for a project, sorted by id (oldest first)."""
    missions_dir = paths.project_dir(project_id) / "missions"
    if not missions_dir.is_dir():
        return []
    out: list[MissionMeta | ParallelMissionMeta] = []
    for d in sorted(missions_dir.iterdir()):
        if not d.is_dir():
            continue
        meta = _load_any_meta(project_id, d.name)
        if meta is not None:
            out.append(meta)
    return out
