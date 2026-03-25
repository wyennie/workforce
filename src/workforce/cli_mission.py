"""CLI commands for missions: dispatch, list, show, replay, clean, prune."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import typer
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
from rich.panel import Panel
from rich.table import Table

from workforce import mission, output, paths, project as project_mod
from workforce.mission import MissionMeta, MissionStatus
from workforce.runner import RunLimits
from workforce.specialist import RosterStore
from workforce.worktree import WorktreeManager


def _stores() -> tuple[RosterStore, project_mod.ProjectStore, WorktreeManager]:
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


# ----- live renderer --------------------------------------------------------


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


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


def _summarize_tool_args(name: str, args: dict[str, Any]) -> str:
    """Best-effort one-line preview of the more interesting argument."""
    for key in ("file_path", "path", "command", "pattern", "url", "query"):
        if key in args:
            return f"{key}={args[key]!r}"
    if args:
        first_key = next(iter(args))
        return f"{first_key}={args[first_key]!r}"
    return ""


# ----- dispatch -------------------------------------------------------------


def dispatch_command(
    project_ref: str = typer.Argument(..., help="Project name or id.", metavar="PROJECT"),
    ticket: str = typer.Argument(..., help="Ticket text in quotes."),
    specialist: str | None = typer.Option(
        None, "--specialist", help="Override automatic specialist selection."
    ),
    max_turns: int = typer.Option(50, "--max-turns", help="Hard cap on assistant turns."),
    max_cost: float = typer.Option(5.0, "--max-cost", help="Hard cap on total cost (USD)."),
    max_wall: float = typer.Option(
        1800.0, "--max-wall", help="Hard cap on wall-clock seconds."
    ),
) -> None:
    """Dispatch a mission: pick a specialist, run them in a fresh worktree."""
    roster_store, project_store, worktree_manager = _stores()

    try:
        proj = project_store.resolve(project_ref)
    except project_mod.ProjectError as e:
        output.die(str(e))

    if not Path(proj.repo_path).is_dir():
        output.die(
            f"project {proj.name!r} points at {proj.repo_path}, which is missing. "
            "Did you move the repo?"
        )

    specialist_name = _resolve_specialist(proj, roster_store, specialist)
    spec = roster_store.load(specialist_name)

    output.info(
        f"[bold]dispatching[/bold] {spec.name} on {proj.name}: "
        f"[italic]{_truncate(ticket, 80)}[/italic]"
    )
    output.rule()

    limits = RunLimits(
        max_turns=max_turns, max_budget_usd=max_cost, max_wall_seconds=max_wall
    )

    try:
        meta = asyncio.run(
            mission.dispatch(
                project=proj,
                specialist=spec,
                ticket=ticket,
                roster_store=roster_store,
                project_store=project_store,
                worktree_manager=worktree_manager,
                limits=limits,
                on_message=_make_renderer(),
            )
        )
    except KeyboardInterrupt:
        output.warn("interrupted")
        raise typer.Exit(code=130)

    output.rule()
    _print_summary(meta)

    if meta.status is not MissionStatus.COMPLETED:
        raise typer.Exit(code=1)


def _print_summary(meta: mission.MissionMeta) -> None:
    style = {
        MissionStatus.COMPLETED: "[green]completed[/green]",
        MissionStatus.ERROR: "[red]error[/red]",
        MissionStatus.WALL_TIMEOUT: "[yellow]wall_timeout[/yellow]",
        MissionStatus.INTERRUPTED: "[yellow]interrupted[/yellow]",
        MissionStatus.TRAILER_VIOLATION: "[red]trailer_violation[/red]",
    }
    output.info(f"mission {meta.mission_id}: {style[meta.status]}")
    output.info(f"  branch:    {meta.branch}")
    output.info(f"  worktree:  {meta.worktree_path}")
    output.info(f"  duration:  {meta.duration_seconds:.1f}s")
    output.info(f"  cost:      ${meta.cost_usd:.4f}")
    output.info(f"  turns:     {meta.turn_count}")
    output.info(f"  commits:   {len(meta.commits)}")
    if meta.commits and len(meta.commits) < 2:
        output.warn(
            "  only one commit — check if the specialist is committing as it goes"
        )
    if meta.error_detail:
        output.fail(f"  detail:    {meta.error_detail}")
    if meta.memory_delta_captured:
        output.info("  memory delta captured")
    output.info(
        f"  artifacts: {paths.project_dir(meta.project_id) / 'missions' / meta.mission_id}"
    )


# ----- mission lookup -------------------------------------------------------


def _load_meta(project_id: str, mission_id: str) -> MissionMeta | None:
    mp = mission.mission_paths(project_id, mission_id)
    if not mp.meta.is_file():
        return None
    return MissionMeta.model_validate_json(mp.meta.read_text())


def _find_mission(mission_id: str) -> tuple[project_mod.Project, MissionMeta]:
    """Locate a mission across all registered projects."""
    pstore = project_mod.ProjectStore()
    for proj in pstore.list():
        meta = _load_meta(proj.id, mission_id)
        if meta is not None:
            return proj, meta
    output.die(f"no mission with id {mission_id!r} found in any project")
    raise AssertionError("unreachable")  # pragma: no cover


def _list_project_missions(project_id: str) -> list[MissionMeta]:
    missions_dir = paths.project_dir(project_id) / "missions"
    if not missions_dir.is_dir():
        return []
    out: list[MissionMeta] = []
    for d in sorted(missions_dir.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if meta_path.is_file():
            try:
                out.append(MissionMeta.model_validate_json(meta_path.read_text()))
            except ValueError:
                # Tolerate corrupt or in-progress meta files; skip silently.
                continue
    return out


# ----- missions list (top-level) --------------------------------------------


_STATUS_STYLES = {
    MissionStatus.COMPLETED: "[green]completed[/green]",
    MissionStatus.ERROR: "[red]error[/red]",
    MissionStatus.WALL_TIMEOUT: "[yellow]wall_timeout[/yellow]",
    MissionStatus.INTERRUPTED: "[yellow]interrupted[/yellow]",
    MissionStatus.TRAILER_VIOLATION: "[red]trailer_violation[/red]",
}


def missions_command(
    project_ref: str = typer.Argument(..., help="Project name or id.", metavar="PROJECT"),
) -> None:
    """List missions recorded for a project (newest first)."""
    paths.ensure_layout()
    pstore = project_mod.ProjectStore()
    try:
        proj = pstore.resolve(project_ref)
    except project_mod.ProjectError as e:
        output.die(str(e))

    missions = _list_project_missions(proj.id)
    if not missions:
        output.info(f"no missions recorded for {proj.name}")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("mission id")
    table.add_column("when")
    table.add_column("specialist")
    table.add_column("status")
    table.add_column("cost", justify="right")
    table.add_column("ticket", overflow="fold")

    for m in reversed(missions):  # newest first
        table.add_row(
            m.mission_id,
            m.started_at,
            m.specialist,
            _STATUS_STYLES[m.status],
            f"${m.cost_usd:.4f}",
            _truncate(m.ticket, 60),
        )
    output.print_table(table)


# ----- replay (top-level) ---------------------------------------------------


def replay_command(
    mission_id: str = typer.Argument(..., help="Mission id."),
    show_thinking: bool = typer.Option(
        False, "--show-thinking", help="Include thinking blocks."
    ),
) -> None:
    """Pretty-print a mission's events.jsonl."""
    paths.ensure_layout()
    proj, meta = _find_mission(mission_id)
    mp = mission.mission_paths(proj.id, mission_id)
    if not mp.events.is_file():
        output.die(f"no events log at {mp.events}")

    output.info(
        f"[bold]replay {mission_id}[/bold] — {proj.name} / {meta.specialist} "
        f"({meta.started_at}) — status: {_STATUS_STYLES[meta.status]}"
    )
    output.rule()

    with mp.events.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                output.warn(f"  (unparseable line: {line[:80]!r})")
                continue
            _render_replay_event(evt, show_thinking=show_thinking)


def _render_replay_event(evt: dict[str, Any], *, show_thinking: bool) -> None:
    t = evt.get("_type")
    if t == "AssistantMessage":
        for block in evt.get("content") or []:
            btype = type(block).__name__ if not isinstance(block, dict) else None
            if isinstance(block, dict):
                if "text" in block:
                    text = (block["text"] or "").rstrip()
                    if text:
                        output.info(text)
                elif "thinking" in block:
                    if show_thinking:
                        output.info(f"[dim italic]thinking: {block['thinking']!r}[/dim italic]")
                elif "name" in block and "input" in block:
                    args = _summarize_tool_args(block["name"], block.get("input") or {})
                    output.info(f"[dim]→ {block['name']}({_truncate(args, 80)})[/dim]")
    elif t == "UserMessage":
        content = evt.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("is_error"):
                    preview = _truncate(repr(block.get("content")), 120)
                    output.warn(f"  ← tool error: {preview}")
    elif t == "ResultMessage":
        cost = evt.get("total_cost_usd") or 0.0
        output.info(
            f"[dim]turns={evt.get('num_turns')} duration={evt.get('duration_ms')}ms "
            f"cost=${cost:.4f}[/dim]"
        )
    elif t == "SystemMessage":
        if evt.get("subtype") != "init":
            output.info(f"[dim][system:{evt.get('subtype')}][/dim]")


# ----- mission sub-typer (show, clean, prune) -------------------------------


mission_sub = typer.Typer(
    name="mission",
    help="Inspect and clean up individual missions.",
    no_args_is_help=True,
)


@mission_sub.command("show")
def mission_show(mission_id: str = typer.Argument(..., help="Mission id.")) -> None:
    """Show one mission's metadata, ticket, result, and commit list."""
    paths.ensure_layout()
    proj, meta = _find_mission(mission_id)
    mp = mission.mission_paths(proj.id, mission_id)

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("project", f"{proj.name} ({proj.id})")
    grid.add_row("specialist", f"{meta.specialist} ({meta.model})")
    grid.add_row("status", _STATUS_STYLES[meta.status])
    grid.add_row("started", meta.started_at)
    grid.add_row("ended", meta.ended_at)
    grid.add_row("duration", f"{meta.duration_seconds:.1f}s")
    grid.add_row("cost", f"${meta.cost_usd:.4f}")
    grid.add_row("turns", str(meta.turn_count))
    grid.add_row("branch", meta.branch)
    grid.add_row("worktree", meta.worktree_path)
    grid.add_row("commits", str(len(meta.commits)))
    if meta.error_detail:
        grid.add_row("error", meta.error_detail)

    output.raw(Panel(grid, title=f"mission {meta.mission_id}", title_align="left"))

    if mp.ticket.is_file():
        output.raw(Panel(mp.ticket.read_text().rstrip(), title="ticket", title_align="left"))
    if mp.result.is_file():
        output.raw(Panel(mp.result.read_text().rstrip(), title="result", title_align="left"))

    if meta.commits:
        ctable = Table(show_header=True, header_style="bold")
        ctable.add_column("sha")
        ctable.add_column("subject", overflow="fold")
        ctable.add_column("flags")
        for c in meta.commits:
            flags = ", ".join(c.trailer_violations) if c.trailer_violations else ""
            ctable.add_row(c.sha[:8], c.subject, f"[red]{flags}[/red]" if flags else "")
        output.print_table(ctable)


@mission_sub.command("clean")
def mission_clean(
    mission_id: str = typer.Argument(..., help="Mission id."),
    force: bool = typer.Option(
        False, "--force", "-f", help="Remove worktree even if it has uncommitted changes."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Drop the mission's worktree (and its registry entry).

    Keeps mission artifacts (events.jsonl, result.md, meta.json) and the branch.
    The branch lives in the source repo; merge or delete it manually.
    """
    paths.ensure_layout()
    proj, meta = _find_mission(mission_id)
    wt_path = Path(meta.worktree_path)

    if not wt_path.exists():
        output.info(f"worktree already gone: {wt_path}")
        # Still try to prune git's registry.
        try:
            WorktreeManager().prune(Path(proj.repo_path))
        except Exception as e:
            output.warn(f"git worktree prune failed: {e}")
        return

    if not yes:
        confirm = typer.confirm(f"Remove worktree at {wt_path}?", default=False)
        if not confirm:
            output.info("aborted")
            raise typer.Exit()

    wm = WorktreeManager()
    try:
        wm.remove(Path(proj.repo_path), wt_path, force=force)
    except Exception as e:
        output.die(f"worktree removal failed: {e}")
    output.success(f"removed worktree {wt_path}")


_DURATION_RE = re.compile(r"^\s*(\d+)\s*([dhwm])\s*$", re.IGNORECASE)


def _parse_duration(s: str) -> dt.timedelta:
    m = _DURATION_RE.match(s)
    if not m:
        raise typer.BadParameter(
            f"unrecognized duration {s!r}; use forms like 7d, 24h, 2w, 1m (m=30d)"
        )
    n, unit = int(m.group(1)), m.group(2).lower()
    return {
        "h": dt.timedelta(hours=n),
        "d": dt.timedelta(days=n),
        "w": dt.timedelta(weeks=n),
        "m": dt.timedelta(days=n * 30),
    }[unit]


def _parse_iso_z(s: str) -> dt.datetime:
    # MissionMeta writes 'YYYY-MM-DDTHH:MM:SSZ' — fromisoformat in 3.11+
    # accepts 'Z' since 3.11. We require 3.11 anyway.
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


@mission_sub.command("prune")
def mission_prune(
    older_than: str = typer.Option(
        "30d",
        "--older-than",
        help="Drop worktrees for missions older than this (e.g. 7d, 24h, 2w).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List what would be removed without touching anything."
    ),
    keep_failed: bool = typer.Option(
        False,
        "--keep-failed",
        help="Don't prune worktrees from failed missions (they may be useful for debugging).",
    ),
) -> None:
    """Bulk-remove old mission worktrees. Mission logs and branches are kept."""
    paths.ensure_layout()
    threshold = dt.datetime.now(dt.timezone.utc) - _parse_duration(older_than)

    pstore = project_mod.ProjectStore()
    wm = WorktreeManager()
    candidates: list[tuple[project_mod.Project, MissionMeta]] = []
    for proj in pstore.list():
        for meta in _list_project_missions(proj.id):
            try:
                started = _parse_iso_z(meta.started_at)
            except ValueError:
                continue
            if started >= threshold:
                continue
            if keep_failed and meta.status is not MissionStatus.COMPLETED:
                continue
            if Path(meta.worktree_path).exists():
                candidates.append((proj, meta))

    if not candidates:
        output.info("nothing to prune")
        return

    for proj, meta in candidates:
        action = "would remove" if dry_run else "removing"
        output.info(f"{action} {meta.mission_id} ({proj.name}, {meta.started_at})")
        if dry_run:
            continue
        try:
            wm.remove(Path(proj.repo_path), Path(meta.worktree_path), force=True)
        except Exception as e:
            output.warn(f"  failed: {e}")

    if not dry_run:
        # One git worktree prune per repo to clean up registry entries.
        repos_seen: set[str] = set()
        for proj, _ in candidates:
            if proj.repo_path in repos_seen:
                continue
            repos_seen.add(proj.repo_path)
            try:
                wm.prune(Path(proj.repo_path))
            except Exception as e:
                output.warn(f"git worktree prune {proj.repo_path}: {e}")
        output.success(f"pruned {len(candidates)} worktree(s)")
