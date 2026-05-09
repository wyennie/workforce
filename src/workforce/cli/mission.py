"""Mission inspection and dispatch commands: missions list, replay, show, tail, retry, diff.

Also hosts `render_labeled_event`, used by `cli_project` for the project-wide
tail stream.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich import box
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from workforce import mission, output, paths
from workforce import project as project_mod
from workforce.mission import MissionMeta
from workforce.parallel import ParallelMissionMeta
from workforce.runner import RunLimits
from workforce.specialist import RosterStore
from workforce.worktree import WorktreeManager

from ._common import (
    _PARALLEL_STATUS_BADGES,
    _PARALLEL_STATUS_STYLES,
    _STATUS_BADGES,
    _STATUS_STYLES,
    _find_mission,
    _find_mission_dir,
    _list_project_missions,
    _load_any_meta,
    _relative_time,
    _summarize_tool_args,
    _tool_color,
    _truncate,
)
from ._completions import complete_mission_id


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
        output.raw(Panel(
            "[dim]No missions yet.\n\nRun [bold]workforce dispatch[/bold] to start one.[/dim]",
            title=f"[bold]{proj.name}[/bold]",
            title_align="left",
            border_style="dim",
            padding=(1, 2),
        ))
        return

    # Compute total cost across all missions for the summary header.
    total_cost = 0.0
    for m in missions:
        if isinstance(m, ParallelMissionMeta):
            total_cost += m.manager_cost_usd
            for ref in m.sub_missions:
                sub = _load_any_meta(proj.id, ref.mission_id)
                if isinstance(sub, MissionMeta):
                    total_cost += sub.cost_usd + sub.review_cost_usd
        else:
            total_cost += m.cost_usd + m.review_cost_usd

    n = len(missions)
    output.info(
        f"[bold]{proj.name}[/bold]  "
        f"[dim]{n} mission{'s' if n != 1 else ''} · ${total_cost:.2f} total[/dim]"
    )

    table = Table(
        show_header=True,
        header_style="bold dim",
        box=box.SIMPLE_HEAD,
        show_edge=False,
        padding=(0, 1),
    )
    table.add_column("id", style="dim", no_wrap=True)
    table.add_column("specialist", no_wrap=True)
    table.add_column("when", style="dim", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("cost", justify="right", style="dim", no_wrap=True)
    table.add_column("ticket", overflow="fold")

    def _fmt_cost(c: float) -> str:
        return f"${c:.4f}" if c < 0.01 else f"${c:.2f}"

    for m in reversed(missions):  # newest first
        if isinstance(m, ParallelMissionMeta):
            # Compute real total cost: manager + all sub-missions.
            real_cost = m.manager_cost_usd
            specialist_names: list[str] = []
            for ref in m.sub_missions:
                sub = _load_any_meta(proj.id, ref.mission_id)
                if isinstance(sub, MissionMeta):
                    real_cost += sub.cost_usd + sub.review_cost_usd
                    if sub.specialist not in specialist_names:
                        specialist_names.append(sub.specialist)
            spec_display = " · ".join(specialist_names) if specialist_names else "[dim](pending)[/dim]"
            table.add_row(
                m.parent_mission_id,
                spec_display,
                _relative_time(m.started_at),
                _PARALLEL_STATUS_STYLES[m.status],
                _fmt_cost(real_cost),
                _truncate(m.ticket, 60),
            )
        else:
            is_sub = "__" in m.mission_id
            # Indent sub-mission ids visually so their relationship to the
            # parent (one row above, usually) is obvious.
            label = f"  ↳ {m.mission_id}" if is_sub else m.mission_id
            total_mission_cost = m.cost_usd + m.review_cost_usd
            row_style = "dim" if is_sub else ""
            table.add_row(
                label,
                m.specialist,
                _relative_time(m.started_at),
                _STATUS_STYLES[m.status],
                _fmt_cost(total_mission_cost),
                _truncate(m.ticket, 60),
                style=row_style,
            )
    output.print_table(table)


# ----- replay ---------------------------------------------------------------


def replay_command(
    mission_id: str = typer.Argument(..., help="Mission id.", autocompletion=complete_mission_id),
    show_thinking: bool = typer.Option(
        False, "--show-thinking", help="Include thinking blocks."
    ),
) -> None:
    """Pretty-print a mission's events.jsonl."""
    paths.ensure_layout()
    proj, meta = _find_mission(mission_id)
    mp = mission.mission_paths(proj.id, mission_id)
    if not mp.events.is_file():
        if isinstance(meta, ParallelMissionMeta):
            output.die(
                f"{mission_id} is a parent mission — replay each sub-mission "
                "individually:\n  "
                + "\n  ".join(f"workforce replay {s.mission_id}" for s in meta.sub_missions)
            )
        output.die(f"no events log at {mp.events}")

    label = (
        meta.specialist if isinstance(meta, MissionMeta)
        else f"({meta.decomposition_kind.value} parent)"
    )
    status_style = (
        _STATUS_STYLES[meta.status] if isinstance(meta, MissionMeta)
        else _PARALLEL_STATUS_STYLES[meta.status]
    )
    output.info(
        f"[bold]replay {mission_id}[/bold] — {proj.name} / {label} "
        f"({meta.started_at}) — status: {status_style}"
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


def render_labeled_event(
    label: str,
    evt: dict[str, Any],
    *,
    show_thinking: bool,
    label_color: str = "cyan",
) -> None:
    """Render one event line with a per-mission prefix, for the project-tail
    multi-mission stream. Public (no leading underscore) so cli_project can
    import it without crossing private boundaries.

    Filters more aggressively than `_render_replay_event` because output from
    multiple missions is interleaved — we drop SystemMessage init/setup noise
    and tool-result echoes; only assistant text, tool calls, and result
    summaries survive.
    """
    prefix = f"[bold {label_color}][{label}][/bold {label_color}]"
    t = evt.get("_type")
    if t == "AssistantMessage":
        for block in evt.get("content") or []:
            if not isinstance(block, dict):
                continue
            if "text" in block:
                text = (block["text"] or "").rstrip()
                if text:
                    output.info(f"{prefix} {text}")
            elif "thinking" in block:
                if show_thinking:
                    output.info(
                        f"{prefix} [dim italic]thinking: {block['thinking']!r}[/dim italic]"
                    )
                else:
                    output.info(f"{prefix} [dim]  ⟨ thinking ⟩[/dim]")
            elif "name" in block and "input" in block:
                args = _summarize_tool_args(block["name"], block.get("input") or {})
                color = _tool_color(block["name"])
                output.info(
                    f"{prefix}  [{color}]→ {block['name']}[/{color}]"
                    f"[dim]  {_truncate(args, 80)}[/dim]"
                )
    elif t == "UserMessage":
        # Surface tool errors only; successful tool results would drown the stream.
        content = evt.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("is_error"):
                    preview = _truncate(repr(block.get("content")), 120)
                    output.warn(f"{prefix} ← tool error: {preview}")
    elif t == "ResultMessage":
        cost = evt.get("total_cost_usd") or 0.0
        is_error = evt.get("is_error", False)
        status_color = "red" if is_error else "green"
        symbol = "✗" if is_error else "✓"
        output.info(
            f"{prefix} [{status_color}]{symbol}[/{status_color}] mission ended  "
            f"[dim]turns={evt.get('num_turns')}  "
            f"duration={evt.get('duration_ms', 0) // 1000:.0f}s  "
            f"cost=${cost:.4f}[/dim]"
        )


def _render_replay_event(
    evt: dict[str, Any],
    *,
    show_thinking: bool,
    _elapsed: Callable[[], str] | None = None,
) -> None:
    """Render a single deserialized event line to the terminal during replay."""
    t = evt.get("_type")
    if t == "AssistantMessage":
        # Emit a turn separator before each assistant turn.
        elapsed_str = _elapsed() if _elapsed is not None else ""
        sep = f"[dim]─── {elapsed_str} ──[/dim]" if elapsed_str else "[dim]───[/dim]"
        output.info(sep)
        for block in evt.get("content") or []:
            if isinstance(block, dict):
                if "text" in block:
                    text = (block["text"] or "").rstrip()
                    if text:
                        output.info(text)
                elif "thinking" in block:
                    if show_thinking:
                        output.raw(Panel(
                            block["thinking"].strip() if block.get("thinking") else "",
                            title="[dim]thinking[/dim]",
                            title_align="left",
                            border_style="dim",
                            padding=(0, 1),
                        ))
                    else:
                        output.info("[dim]  ⟨ thinking ⟩[/dim]")
                elif "name" in block and "input" in block:
                    args = _summarize_tool_args(block["name"], block.get("input") or {})
                    color = _tool_color(block["name"])
                    output.info(
                        f"  [{color}]→ {block['name']}[/{color}]"
                        f"[dim]  {_truncate(args, 80)}[/dim]"
                    )
    elif t == "UserMessage":
        content = evt.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("is_error"):
                    preview = _truncate(repr(block.get("content")), 120)
                    output.raw(Panel(
                        f"[red]{preview}[/red]",
                        title="[red]✗ tool error[/red]",
                        title_align="left",
                        border_style="red",
                        padding=(0, 1),
                    ))
    elif t == "ResultMessage":
        cost = evt.get("total_cost_usd") or 0.0
        is_error = evt.get("is_error", False)
        status_color = "red" if is_error else "green"
        symbol = "✗" if is_error else "✓"
        output.info(
            f"[{status_color}]{symbol}[/{status_color}] mission ended  "
            f"[dim]turns={evt.get('num_turns')}  "
            f"duration={evt.get('duration_ms', 0) // 1000:.0f}s  "
            f"cost=${cost:.4f}[/dim]"
        )
        output.rule()
    elif t == "SystemMessage":
        if evt.get("subtype") != "init":
            output.info(f"[dim][system:{evt.get('subtype')}][/dim]")


# ----- show -----------------------------------------------------------------


def mission_show(mission_id: str = typer.Argument(..., help="Mission id.", autocompletion=complete_mission_id)) -> None:
    """Show one mission's details. Works for single, parent (parallel), or sub missions."""
    paths.ensure_layout()
    proj, meta = _find_mission(mission_id)
    if isinstance(meta, ParallelMissionMeta):
        _show_parent_meta(proj, meta)
    else:
        _show_single_meta(proj, meta)


def _show_single_meta(proj: project_mod.Project, meta: MissionMeta) -> None:
    """Render the detail view for a single (non-parallel) mission."""
    from workforce.mission import MissionStatus

    mp = mission.mission_paths(proj.id, meta.mission_id)

    border_color = {
        MissionStatus.COMPLETED: "green",
        MissionStatus.ERROR: "red",
        MissionStatus.REVIEW_REJECTED: "red",
        MissionStatus.WALL_TIMEOUT: "yellow",
        MissionStatus.INTERRUPTED: "yellow",
    }.get(meta.status, "dim")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold dim")
    grid.add_column()
    grid.add_row("project", f"{proj.name}  [dim]({proj.id})[/dim]")
    grid.add_row("specialist", f"{meta.specialist}  [dim]({meta.model})[/dim]")
    grid.add_row("status", _STATUS_BADGES.get(meta.status, _STATUS_STYLES[meta.status]))
    grid.add_row("started", _relative_time(meta.started_at))
    # Human-friendly duration formatting.
    dur = meta.duration_seconds
    if dur >= 60:
        grid.add_row("duration", f"{int(dur) // 60}m {int(dur) % 60}s")
    else:
        grid.add_row("duration", f"{dur:.0f}s")
    total_cost = meta.cost_usd + meta.review_cost_usd
    grid.add_row(
        "cost",
        f"${total_cost:.4f}  "
        f"[dim](specialist ${meta.cost_usd:.4f}"
        + (f"  review ${meta.review_cost_usd:.4f}" if meta.review_cost_usd else "")
        + "[/dim])",
    )
    grid.add_row("turns", str(meta.turn_count))
    if meta.branch is None:
        grid.add_row("workspace", meta.worktree_path or "(unknown)")
    else:
        grid.add_row("branch", meta.branch)
        grid.add_row("worktree", meta.worktree_path or "(removed)")
        grid.add_row("commits", str(len(meta.commits)))
    if meta.error_detail:
        grid.add_row("error", f"[red]{meta.error_detail}[/red]")

    output.raw(Panel(
        grid,
        title=f"[bold]mission[/bold] [dim]{meta.mission_id}[/dim]",
        title_align="left",
        border_style=border_color,
        padding=(0, 1),
    ))

    # Review verdict panel (when applicable).
    if meta.reviews:
        last = meta.reviews[-1]
        verdict = (
            "[bold green]✓ approved[/bold green]"
            if last.approved
            else "[bold red]✗ rejected[/bold red]"
        )
        review_grid = Table.grid(padding=(0, 2))
        review_grid.add_column(style="bold dim")
        review_grid.add_column()
        review_grid.add_row("verdict", verdict)
        review_grid.add_row("rounds", str(len(meta.reviews)))
        review_grid.add_row("revisions", str(meta.revision_rounds))
        review_grid.add_row("cost", f"${meta.review_cost_usd:.4f}")
        if not last.approved and last.issues:
            bullets = "\n".join(f"• {i}" for i in last.issues[:5])
            review_grid.add_row("issues", f"[red]{bullets}[/red]")
        review_border = "green" if last.approved else "red"
        output.raw(Panel(
            review_grid,
            title="[bold]review[/bold]",
            title_align="left",
            border_style=review_border,
            padding=(0, 1),
        ))

    if mp.ticket.is_file():
        output.raw(Panel(
            Markdown(mp.ticket.read_text().rstrip()),
            title="[bold]ticket[/bold]",
            title_align="left",
            border_style="dim",
            padding=(0, 1),
        ))
    if mp.result.is_file():
        output.raw(Panel(
            Markdown(mp.result.read_text().rstrip()),
            title="[bold]result[/bold]",
            title_align="left",
            border_style="blue",
            padding=(0, 1),
        ))

    if meta.commits:
        ctable = Table(show_header=True, header_style="bold dim", box=box.SIMPLE)
        ctable.add_column("sha", style="dim", width=9)
        ctable.add_column("subject", overflow="fold")
        for c in meta.commits:
            ctable.add_row(c.sha[:8], c.subject)
        output.raw(Panel(
            ctable,
            title="[bold]commits[/bold]",
            title_align="left",
            border_style="dim",
            padding=(0, 0),
        ))


def _show_parent_meta(proj: project_mod.Project, parent: ParallelMissionMeta) -> None:
    """Render a parent (parallel) mission with its sub-mission roll-up."""
    from workforce.parallel import ParallelStatus

    mp = mission.mission_paths(proj.id, parent.parent_mission_id)

    border_color = {
        ParallelStatus.COMPLETED: "green",
        ParallelStatus.FAILED: "red",
        ParallelStatus.PARTIAL: "yellow",
        ParallelStatus.DISPATCHED: "yellow",
    }.get(parent.status, "dim")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold dim")
    grid.add_column()
    grid.add_row("project", f"{proj.name}  [dim]({proj.id})[/dim]")
    grid.add_row("kind", parent.decomposition_kind.value)
    grid.add_row("status", _PARALLEL_STATUS_BADGES.get(parent.status, _PARALLEL_STATUS_STYLES[parent.status]))
    grid.add_row("started", _relative_time(parent.started_at))
    grid.add_row("ended", _relative_time(parent.ended_at) if parent.ended_at else "[dim](in progress)[/dim]")
    grid.add_row("sub-missions", str(len(parent.sub_missions)))
    if parent.merge_order:
        grid.add_row("merge order", " → ".join(parent.merge_order))

    output.raw(Panel(
        grid,
        title=f"[bold]parent mission[/bold] [dim]{parent.parent_mission_id}[/dim]",
        title_align="left",
        border_style=border_color,
        padding=(0, 1),
    ))

    if mp.ticket.is_file():
        output.raw(Panel(
            Markdown(mp.ticket.read_text().rstrip()),
            title="[bold]ticket[/bold]",
            title_align="left",
            border_style="dim",
            padding=(0, 1),
        ))

    decomp_path = mp.root / "decomposition.json"
    contract_path = mp.root / "contract" / "contract.md"
    if contract_path.is_file():
        output.raw(Panel(
            Markdown(contract_path.read_text().rstrip()),
            title="[bold]contract[/bold]",
            title_align="left",
            border_style="dim",
            padding=(0, 1),
        ))
    if decomp_path.is_file():
        output.info(f"[dim]decomposition.json: {decomp_path}[/dim]")

    # Roll up sub-missions
    if parent.sub_missions:
        stable = Table(show_header=True, header_style="bold dim", box=box.SIMPLE)
        stable.add_column("task")
        stable.add_column("specialist")
        stable.add_column("mission id", overflow="fold")
        stable.add_column("status")
        stable.add_column("cost", justify="right")
        stable.add_column("turns", justify="right")
        stable.add_column("commits", justify="right")
        total_cost = parent.manager_cost_usd
        for ref in parent.sub_missions:
            sub = _load_any_meta(proj.id, ref.mission_id)
            if isinstance(sub, MissionMeta):
                total_cost += sub.cost_usd + sub.review_cost_usd
                stable.add_row(
                    ref.task_id, sub.specialist, ref.mission_id,
                    _STATUS_STYLES[sub.status],
                    f"${sub.cost_usd + sub.review_cost_usd:.4f}",
                    str(sub.turn_count),
                    str(len(sub.commits)),
                )
            else:
                stable.add_row(
                    ref.task_id, ref.specialist, ref.mission_id,
                    "[red]missing meta[/red]", "—", "—", "—",
                )
        output.raw(Panel(
            stable,
            title=f"[bold]sub-missions[/bold]  [dim]manager ${parent.manager_cost_usd:.4f}  total ${total_cost:.4f}[/dim]",
            title_align="left",
            border_style="dim",
            padding=(0, 0),
        ))

    drifters = [(s.task_id, s.out_of_lane_files) for s in parent.sub_missions if s.out_of_lane_files]
    if drifters:
        atable = Table(show_header=True, header_style="bold dim", box=box.SIMPLE)
        atable.add_column("task")
        atable.add_column("files written outside owns_paths", overflow="fold")
        for task_id, files in drifters:
            atable.add_row(task_id, "\n".join(files))
        output.raw(Panel(
            atable,
            title="[red]decomposition drift[/red]",
            title_align="left",
            border_style="red",
            padding=(0, 0),
        ))


# ----- tail -----------------------------------------------------------------


def mission_tail(
    mission_id: str = typer.Argument(..., help="Mission id.", autocompletion=complete_mission_id),
    show_thinking: bool = typer.Option(
        False, "--show-thinking", help="Include thinking blocks."
    ),
    follow: bool = typer.Option(
        True, "--follow/--no-follow", "-f",
        help="Keep watching for new events. Pass --no-follow to print existing events and exit.",
    ),
    poll_seconds: float = typer.Option(
        0.5, "--poll", help="How often to check for new events (seconds).",
    ),
    timeout: float = typer.Option(
        0, "--timeout",
        help="Exit with an error if the mission does not finish within SECONDS. "
             "0 = disabled (default). Useful for CI wrappers.",
    ),
) -> None:
    """Pretty-print a mission's events.jsonl as it's appended (or once, with --no-follow)."""
    paths.ensure_layout()

    # The mission may not have written meta.json yet (in-progress, freshly
    # spawned by `dispatch --window`). Fall back to a directory scan so tail
    # can attach to a mission the moment its dir exists.
    found = _find_mission_dir(mission_id)
    if found is None:
        # Wait briefly for the dispatch subprocess to create the dir, then bail.
        import time as _time
        for _ in range(40):  # ~10s
            _time.sleep(0.25)
            found = _find_mission_dir(mission_id)
            if found is not None:
                break
    if found is None:
        output.die(f"no mission with id {mission_id!r} found in any project")
    proj, mp = found

    # If the events file is missing and startup.log exists, the background
    # process likely crashed at startup — show it as a diagnostic hint.
    startup_log = mp.root / "startup.log"
    if not mp.events.is_file() and startup_log.is_file():
        hint = startup_log.read_text().strip()
        if hint:
            output.warn(
                f"[yellow]startup.log for {mission_id} suggests an early failure:[/yellow]\n{hint}"
            )

    meta = _load_any_meta(proj.id, mission_id)
    if isinstance(meta, ParallelMissionMeta):
        output.die(
            f"{mission_id} is a parent mission — tail each sub-mission individually:\n  "
            + "\n  ".join(f"workforce mission tail {s.mission_id}" for s in meta.sub_missions)
        )

    label: str
    if isinstance(meta, MissionMeta):
        label = meta.specialist
    elif meta is None:
        label = "(running)"
    else:
        label = f"({meta.decomposition_kind.value} parent)"

    # Render a structured header panel instead of a plain info line.
    header = Table.grid(padding=(0, 3))
    header.add_column(style="bold dim")
    header.add_column()
    header.add_row("mission", mission_id)
    header.add_row("specialist", label)
    header.add_row("project", proj.name)
    output.raw(Panel(
        header,
        title="[bold cyan]● live tail[/bold cyan]",
        title_align="left",
        border_style="cyan",
        padding=(0, 1),
    ))

    import time as _time
    _tail_start = _time.monotonic()

    def _elapsed() -> str:
        s = int(_time.monotonic() - _tail_start)
        return f"{s // 60:02d}:{s % 60:02d}"

    pos = 0
    result_seen = False
    start_time = _time.time() if timeout > 0 else None
    try:
        while True:
            try:
                with mp.events.open() as f:
                    f.seek(pos)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        _render_replay_event(
                            evt, show_thinking=show_thinking, _elapsed=_elapsed
                        )
                        if evt.get("_type") == "ResultMessage":
                            result_seen = True
                    pos = f.tell()
            except FileNotFoundError:
                pass
            if not follow:
                return
            # Stop following once a ResultMessage closes the mission.
            # Sleep one extra poll cycle to catch any trailing events written
            # in the same batch (e.g. memory-delta call), then exit cleanly.
            if result_seen:
                _time.sleep(poll_seconds)
                return
            # Timeout guard: exit with an error if the mission takes too long.
            if start_time is not None and _time.time() - start_time > timeout:
                output.die(
                    f"timeout: mission {mission_id!r} did not finish within {timeout:.0f}s"
                )
            _time.sleep(poll_seconds)
    except KeyboardInterrupt:
        output.info("[dim](stopped)[/dim]")


# ----- retry ----------------------------------------------------------------


def retry_command(
    mission_id: str = typer.Argument(..., help="Mission id to retry."),
    background: bool = typer.Option(
        False, "--background",
        help="Background the re-dispatched mission and return immediately.",
    ),
) -> None:
    """Re-dispatch a past mission with the same ticket and specialist."""
    paths.ensure_layout()
    proj, meta = _find_mission(mission_id)
    mp = mission.mission_paths(proj.id, mission_id)

    if not mp.ticket.is_file():
        output.die(f"no ticket.md found for mission {mission_id}")
    ticket_text = mp.ticket.read_text()

    roster_store = RosterStore()
    project_store = project_mod.ProjectStore()
    worktree_manager = WorktreeManager()
    limits = RunLimits()

    if isinstance(meta, MissionMeta):
        # Single mission (direct-specialist or manager-picked single) — re-dispatch
        # with the same specialist.
        if not roster_store.exists(meta.specialist):
            output.die(f"specialist {meta.specialist!r} no longer exists in the roster")
        spec = roster_store.load(meta.specialist)

        if background:
            # Lazy import avoids a circular dependency at module-load time.
            from workforce.cli.dispatch import _dispatch_detached  # noqa: PLC0415
            _dispatch_detached(
                project_ref=proj.name,
                ticket=ticket_text,
                specialist=meta.specialist,
                mission_id_override=None,
                max_turns=limits.max_turns,
                max_cost=limits.max_budget_usd,
                max_wall=limits.max_wall_seconds,
                review=False,
                max_revisions=3,
                open_window=False,
            )
        else:
            new_mission_id = mission.generate_mission_id()
            output.info(f"retrying as mission [bold]{new_mission_id}[/bold]")
            from workforce.cli.dispatch import _dispatch_direct  # noqa: PLC0415
            _dispatch_direct(
                proj, ticket_text, spec,
                roster_store, project_store, worktree_manager, limits,
                mission_id=new_mission_id,
            )

    elif isinstance(meta, ParallelMissionMeta):
        if background:
            output.die("--background is not supported for parallel parent mission retries")
        from workforce.cli.dispatch import _dispatch_with_manager  # noqa: PLC0415
        _dispatch_with_manager(
            proj, ticket_text, roster_store, project_store, worktree_manager,
            limits=limits, skip_confirm=False, auto_staff=True,
        )


# ----- diff -----------------------------------------------------------------


def diff_command(
    mission_id: str = typer.Argument(..., help="Mission id."),
    stat: bool = typer.Option(
        False, "--stat", help="Show diffstat instead of full diff.",
    ),
) -> None:
    """Show the git diff between a mission's base SHA and HEAD in its worktree.

    For parallel parent missions, iterates over each sub-mission and shows a
    labelled header followed by that sub-mission's diff.
    """
    paths.ensure_layout()
    proj, meta = _find_mission(mission_id)

    if isinstance(meta, ParallelMissionMeta):
        for ref in meta.sub_missions:
            output.rule(f"{ref.task_id} ({ref.mission_id})")
            sub_meta = _load_any_meta(proj.id, ref.mission_id)
            if isinstance(sub_meta, MissionMeta):
                _run_git_diff(sub_meta, stat=stat)
            else:
                output.warn(f"  no meta for sub-mission {ref.mission_id}")
    else:
        _run_git_diff(meta, stat=stat)


def _run_git_diff(meta: MissionMeta, *, stat: bool) -> None:
    """Run ``git diff {base_sha}..HEAD`` in the mission's worktree."""
    if meta.base_sha is None or meta.worktree_path is None:
        output.warn(
            f"  mission {meta.mission_id}: no base_sha or worktree_path "
            "(workspace mission — no git diff available)"
        )
        return

    worktree = Path(meta.worktree_path)
    if not worktree.is_dir():
        output.warn(f"  worktree {worktree} no longer exists")
        return

    cmd = ["git", "diff"]
    if stat:
        cmd.append("--stat")
    cmd.append(f"{meta.base_sha}..HEAD")

    subprocess.run(cmd, cwd=worktree)
