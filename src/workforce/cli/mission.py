"""Read-only mission inspection commands: missions list, replay, show, tail.

Also hosts `render_labeled_event`, used by `cli_project` for the project-wide
tail stream.
"""

from __future__ import annotations

import json
from typing import Any

import typer
from rich.panel import Panel
from rich.table import Table

from workforce import mission, output, paths
from workforce import project as project_mod
from workforce.mission import MissionMeta
from workforce.parallel import ParallelMissionMeta

from ._common import (
    _PARALLEL_STATUS_STYLES,
    _STATUS_STYLES,
    _find_mission,
    _find_mission_dir,
    _list_project_missions,
    _load_any_meta,
    _summarize_tool_args,
    _truncate,
)


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
    table.add_column("kind")
    table.add_column("when")
    table.add_column("specialist / tasks", overflow="fold")
    table.add_column("status")
    table.add_column("cost", justify="right")
    table.add_column("ticket", overflow="fold")

    for m in reversed(missions):  # newest first
        if isinstance(m, ParallelMissionMeta):
            tasks = ", ".join(s.task_id for s in m.sub_missions) or "(none)"
            table.add_row(
                m.parent_mission_id,
                f"[bold]{m.decomposition_kind.value}[/bold]",
                m.started_at,
                f"[dim]{tasks}[/dim]",
                _PARALLEL_STATUS_STYLES[m.status],
                f"${m.manager_cost_usd:.4f}",
                _truncate(m.ticket, 60),
            )
        else:
            kind = "sub" if "__" in m.mission_id else "single"
            # Indent sub-mission ids visually so their relationship to the
            # parent (one row above, usually) is obvious.
            label = f"  ↳ {m.mission_id}" if kind == "sub" else m.mission_id
            table.add_row(
                label,
                f"[dim]{kind}[/dim]",
                m.started_at,
                m.specialist,
                _STATUS_STYLES[m.status],
                f"${m.cost_usd:.4f}",
                _truncate(m.ticket, 60),
            )
    output.print_table(table)


# ----- replay ---------------------------------------------------------------


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


def render_labeled_event(label: str, evt: dict[str, Any], *, show_thinking: bool) -> None:
    """Render one event line with a per-mission prefix, for the project-tail
    multi-mission stream. Public (no leading underscore) so cli_project can
    import it without crossing private boundaries.

    Filters more aggressively than `_render_replay_event` because output from
    multiple missions is interleaved — we drop SystemMessage init/setup noise
    and tool-result echoes; only assistant text, tool calls, and result
    summaries survive.
    """
    prefix = f"[bold cyan][{label}][/bold cyan]"
    t = evt.get("_type")
    if t == "AssistantMessage":
        for block in evt.get("content") or []:
            if not isinstance(block, dict):
                continue
            if "text" in block:
                text = (block["text"] or "").rstrip()
                if text:
                    output.info(f"{prefix} {text}")
            elif "thinking" in block and show_thinking:
                output.info(
                    f"{prefix} [dim italic]thinking: {block['thinking']!r}[/dim italic]"
                )
            elif "name" in block and "input" in block:
                args = _summarize_tool_args(block["name"], block.get("input") or {})
                output.info(f"{prefix} [dim]→ {block['name']}({_truncate(args, 80)})[/dim]")
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
        output.info(
            f"{prefix} [dim]turns={evt.get('num_turns')} "
            f"cost=${cost:.4f} (mission ended)[/dim]"
        )


def _render_replay_event(evt: dict[str, Any], *, show_thinking: bool) -> None:
    """Render a single deserialized event line to the terminal during replay."""
    t = evt.get("_type")
    if t == "AssistantMessage":
        for block in evt.get("content") or []:
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


# ----- show -----------------------------------------------------------------


def mission_show(mission_id: str = typer.Argument(..., help="Mission id.")) -> None:
    """Show one mission's details. Works for single, parent (parallel), or sub missions."""
    paths.ensure_layout()
    proj, meta = _find_mission(mission_id)
    if isinstance(meta, ParallelMissionMeta):
        _show_parent_meta(proj, meta)
    else:
        _show_single_meta(proj, meta)


def _show_single_meta(proj: project_mod.Project, meta: MissionMeta) -> None:
    """Render the detail view for a single (non-parallel) mission."""
    mp = mission.mission_paths(proj.id, meta.mission_id)
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
    if meta.branch is None:
        grid.add_row("workspace", meta.worktree_path or "(unknown)")
    else:
        grid.add_row("branch", meta.branch)
        grid.add_row("worktree", meta.worktree_path or "(unknown)")
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
        for c in meta.commits:
            ctable.add_row(c.sha[:8], c.subject)
        output.print_table(ctable)


def _show_parent_meta(proj: project_mod.Project, parent: ParallelMissionMeta) -> None:
    """Render a parent (parallel) mission with its sub-mission roll-up."""
    mp = mission.mission_paths(proj.id, parent.parent_mission_id)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("project", f"{proj.name} ({proj.id})")
    grid.add_row("kind", parent.decomposition_kind.value)
    grid.add_row("status", _PARALLEL_STATUS_STYLES[parent.status])
    grid.add_row("started", parent.started_at)
    grid.add_row("ended", parent.ended_at or "(in progress / crashed)")
    grid.add_row("manager cost", f"${parent.manager_cost_usd:.4f}")
    grid.add_row("sub-missions", str(len(parent.sub_missions)))
    if parent.merge_order:
        grid.add_row("merge order", " → ".join(parent.merge_order))

    output.raw(Panel(grid, title=f"parent mission {parent.parent_mission_id}", title_align="left"))

    if mp.ticket.is_file():
        output.raw(Panel(mp.ticket.read_text().rstrip(), title="ticket", title_align="left"))

    decomp_path = mp.root / "decomposition.json"
    contract_path = mp.root / "contract" / "contract.md"
    if contract_path.is_file():
        output.raw(Panel(
            contract_path.read_text().rstrip(),
            title="contract", title_align="left",
        ))
    if decomp_path.is_file():
        output.info(f"[dim]decomposition.json: {decomp_path}[/dim]")

    # Roll up sub-missions
    if parent.sub_missions:
        stable = Table(show_header=True, header_style="bold")
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
                total_cost += sub.cost_usd
                stable.add_row(
                    ref.task_id, sub.specialist, ref.mission_id,
                    _STATUS_STYLES[sub.status],
                    f"${sub.cost_usd:.4f}",
                    str(sub.turn_count),
                    str(len(sub.commits)),
                )
            else:
                stable.add_row(
                    ref.task_id, ref.specialist, ref.mission_id,
                    "[red]missing meta[/red]", "—", "—", "—",
                )
        output.print_table(stable)
        output.info(f"  total cost (manager + subs): ${total_cost:.4f}")

    drifters = [(s.task_id, s.out_of_lane_files) for s in parent.sub_missions if s.out_of_lane_files]
    if drifters:
        atable = Table(show_header=True, header_style="bold")
        atable.add_column("task")
        atable.add_column("files written outside owns_paths", overflow="fold")
        for task_id, files in drifters:
            atable.add_row(task_id, "\n".join(files))
        output.raw(Panel(
            atable,
            title="[red]decomposition drift[/red]",
            title_align="left",
        ))


# ----- tail -----------------------------------------------------------------


def mission_tail(
    mission_id: str = typer.Argument(..., help="Mission id."),
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
    output.info(
        f"[bold]tailing {mission_id}[/bold] — {proj.name} / {label}  "
        f"[dim]({mp.events})[/dim]"
    )
    output.rule()

    import time
    pos = 0
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
                        _render_replay_event(evt, show_thinking=show_thinking)
                    pos = f.tell()
            except FileNotFoundError:
                pass
            if not follow:
                return
            # Stop following once a ResultMessage closes the mission, but keep
            # tailing through the memory-delta call's events if any.
            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        output.info("[dim](stopped)[/dim]")
