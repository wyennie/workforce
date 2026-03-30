"""CLI commands for missions: dispatch, list, show, replay, clean, prune."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import subprocess
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

from workforce import (
    cli_panels,
    manager,
    mission,
    output,
    parallel,
    paths,
)
from workforce import (
    project as project_mod,
)
from workforce import (
    specialist as specialist_mod,
)
from workforce.manager import (
    Decomposition,
    DecompositionKind,
    ManagerError,
    ValidationError,
)
from workforce.mission import MissionMeta, MissionStatus
from workforce.parallel import (
    ParallelMissionMeta,
    ParallelStatus,
    ResolutionError,
    merge_plan,
)
from workforce.runner import RunLimits
from workforce.specialist import RosterStore
from workforce.worktree import (
    WorktreeError,
    WorktreeManager,
    find_workforce_branches,
    has_commits,
    is_repo_clean,
)


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
        None,
        "--specialist",
        help="Bypass the Manager and dispatch this specialist directly. Use for tiny tickets where you don't need planning overhead.",
    ),
    auto_staff: bool = typer.Option(
        True,
        "--auto-staff/--no-auto-staff",
        help="Let the Manager auto-assign roster members or auto-hire from templates as needed. Default on.",
    ),
    auto_merge: bool = typer.Option(
        False,
        "--auto-merge/--no-auto-merge",
        help="After successful completion, run the merge plan against the source repo's current branch. Aborts on conflict.",
    ),
    merge_into: str | None = typer.Option(
        None,
        "--merge-into",
        metavar="BRANCH",
        help="After successful completion, switch to BRANCH and merge there. Implies --auto-merge with an explicit target.",
    ),
    max_turns: int = typer.Option(50, "--max-turns", help="Hard cap on assistant turns per sub-mission."),
    max_cost: float = typer.Option(5.0, "--max-cost", help="Hard cap on cost (USD) per sub-mission."),
    max_wall: float = typer.Option(
        1800.0, "--max-wall", help="Hard cap on wall-clock seconds per sub-mission."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Skip the decomposition confirmation prompt.",
    ),
    panels: bool = typer.Option(
        False, "--panels",
        help="Show per-worker live panels instead of the default interleaved output (parallel mode only).",
    ),
    review: bool = typer.Option(
        False, "--review",
        help="After each sub-mission, run the Reviewer. On rejection, the specialist re-runs with the Reviewer's feedback (up to --max-revisions rounds).",
    ),
    max_revisions: int = typer.Option(
        3, "--max-revisions",
        help="Hard cap on Reviewer rejection loops per sub-mission. Only applies with --review.",
    ),
) -> None:
    """Dispatch a mission. The Manager plans it, then it runs.

    The Manager runs first to decide whether the ticket should fan out across
    multiple specialists in parallel, run as a sequential chain, or just go
    to one specialist. Pass --specialist to skip the Manager and dispatch
    directly to a named specialist (cheaper for tiny tickets).
    """
    roster_store, project_store, worktree_manager = _stores()

    try:
        proj = project_store.resolve(project_ref)
    except project_mod.ProjectError as e:
        output.die(str(e))

    repo_path = Path(proj.repo_path)
    if not repo_path.is_dir():
        output.die(
            f"project {proj.name!r} points at {proj.repo_path}, which is missing. "
            "Did you move the repo?"
        )

    # Preflight: the source repo needs at least one commit and a clean tree.
    # Cheap to check, expensive to discover after the Manager has run.
    if not has_commits(repo_path):
        output.die(
            f"{proj.repo_path} has no commits yet. Run "
            f"`git -C {proj.repo_path} commit --allow-empty -m initial` first."
        )
    clean, dirty_paths = is_repo_clean(repo_path)
    if not clean:
        preview = ", ".join(dirty_paths[:3]) + ("..." if len(dirty_paths) > 3 else "")
        output.die(
            f"{proj.repo_path} has uncommitted changes ({preview}). "
            "Commit or stash before dispatching. (Untracked files are OK.)"
        )

    limits = RunLimits(
        max_turns=max_turns, max_budget_usd=max_cost, max_wall_seconds=max_wall
    )

    # Bypass: --specialist X skips the Manager entirely.
    if specialist is not None:
        if not roster_store.exists(specialist):
            output.die(f"no such specialist: {specialist!r}")
        if specialist not in proj.assigned_specialists:
            output.die(
                f"{specialist!r} isn't assigned to {proj.name}. "
                f"Run `workforce project assign {proj.name} {specialist}` first."
            )
        _dispatch_direct(
            proj, ticket, roster_store.load(specialist),
            roster_store, project_store, worktree_manager, limits,
            auto_merge=auto_merge or merge_into is not None,
            merge_into=merge_into,
            review=review, max_revisions=max_revisions,
        )
        return

    # Default: Manager plans, then we route based on its decision.
    _dispatch_with_manager(
        proj, ticket, roster_store, project_store, worktree_manager,
        limits=limits, skip_confirm=yes, auto_staff=auto_staff,
        auto_merge=auto_merge or merge_into is not None,
        merge_into=merge_into,
        panels=panels,
        review=review, max_revisions=max_revisions,
    )


def _dispatch_direct(
    proj: project_mod.Project,
    ticket: str,
    spec: specialist_mod.Specialist,
    roster_store: RosterStore,
    project_store: project_mod.ProjectStore,
    worktree_manager: WorktreeManager,
    limits: RunLimits,
    *,
    auto_merge: bool = False,
    merge_into: str | None = None,
    review: bool = False,
    max_revisions: int = 3,
) -> None:
    """Single specialist, no Manager. The --specialist X bypass."""
    output.info(
        f"[bold]dispatching[/bold] {spec.name} on {proj.name}: "
        f"[italic]{_truncate(ticket, 80)}[/italic]  [dim](no manager)[/dim]"
    )
    output.rule()
    try:
        meta = asyncio.run(
            mission.dispatch(
                project=proj, specialist=spec, ticket=ticket,
                roster_store=roster_store, project_store=project_store,
                worktree_manager=worktree_manager, limits=limits,
                on_message=_make_renderer(),
                review=review, max_revisions=max_revisions,
            )
        )
    except KeyboardInterrupt:
        output.warn("interrupted")
        raise typer.Exit(code=130) from None
    except WorktreeError as e:
        output.die(str(e))
    output.rule()
    _print_summary(meta)
    if auto_merge:
        _run_auto_merge_single(proj, meta, target=merge_into)
    if meta.status is not MissionStatus.COMPLETED:
        raise typer.Exit(code=1)


# ----- parallel dispatch -----------------------------------------------------


def _dispatch_with_manager(
    proj: project_mod.Project,
    ticket: str,
    roster_store: RosterStore,
    project_store: project_mod.ProjectStore,
    worktree_manager: WorktreeManager,
    *,
    limits: RunLimits,
    skip_confirm: bool,
    auto_staff: bool,
    auto_merge: bool = False,
    merge_into: str | None = None,
    panels: bool = False,
    review: bool = False,
    max_revisions: int = 3,
) -> None:
    """Run Manager, branch on `kind`: single → mission.dispatch; else parallel."""
    if not proj.assigned_specialists and not auto_staff:
        output.die(
            f"no specialists assigned to project {proj.name!r} and --no-auto-staff. "
            f"Either assign specialists or drop --no-auto-staff so the Manager "
            "can hire from templates as needed."
        )

    output.info(
        f"[bold]dispatching[/bold] on {proj.name}: "
        f"[italic]{_truncate(ticket, 80)}[/italic]"
    )
    output.info("[dim]Manager planning...[/dim]")

    # Run the Manager.
    specs_info = parallel._build_specialist_info(proj, roster_store, project_store)
    try:
        decomp, manager_cost, _ = asyncio.run(
            manager.run_manager(
                ticket=ticket,
                repo_path=Path(proj.repo_path),
                project_specialists=specs_info,
            )
        )
    except KeyboardInterrupt:
        output.warn("interrupted")
        raise typer.Exit(code=130) from None
    except ManagerError as e:
        output.die(f"manager: {e}")

    output.info(
        f"[dim]manager: kind={decomp.kind.value}  cost=${manager_cost:.4f}  "
        f"({decomp.rationale})[/dim]"
    )

    # Branch on kind.
    if decomp.kind is DecompositionKind.SINGLE:
        _dispatch_after_manager_single(
            proj, ticket, decomp, manager_cost,
            roster_store, project_store, worktree_manager,
            limits=limits, auto_staff=auto_staff,
            auto_merge=auto_merge, merge_into=merge_into,
            review=review, max_revisions=max_revisions,
        )
    else:
        _dispatch_after_manager_parallel(
            proj, ticket, decomp, manager_cost,
            roster_store, project_store, worktree_manager,
            limits=limits, skip_confirm=skip_confirm, auto_staff=auto_staff,
            auto_merge=auto_merge, merge_into=merge_into,
            panels=panels,
            review=review, max_revisions=max_revisions,
        )


def _dispatch_after_manager_single(
    proj: project_mod.Project,
    ticket: str,
    decomp: Decomposition,
    manager_cost: float,
    roster_store: RosterStore,
    project_store: project_mod.ProjectStore,
    worktree_manager: WorktreeManager,
    *,
    limits: RunLimits,
    auto_staff: bool,
    auto_merge: bool = False,
    merge_into: str | None = None,
    review: bool = False,
    max_revisions: int = 3,
) -> None:
    """Manager said single. Use its specialist suggestion, dispatch one mission."""
    if not decomp.tasks:
        output.die("manager returned kind=single but no tasks")

    # Resolve the one task's specialist via the same auto-staff path.
    try:
        resolved = parallel.resolve_task_specialists(
            decomp,
            parent_mission_id=mission.generate_mission_id(),
            project=proj,
            roster_store=roster_store,
            project_store=project_store,
            auto_staff=auto_staff,
        )
    except ResolutionError as e:
        output.die(str(e))

    r = resolved[0]
    # For single, drop the __task suffix — it's just one mission, no parent.
    mission_id = mission.generate_mission_id()
    task = r.task

    if r.staffing_action == "auto_hired_from_template":
        output.info(f"[bold magenta]Auto-hired:[/bold magenta] {r.specialist.name} (← {task.template_hint})")
    elif r.staffing_action == "auto_assigned_from_roster":
        output.info(f"[cyan]Auto-assigned:[/cyan] {r.specialist.name}")

    output.info(
        f"[dim]single task → {r.specialist.name} ({mission_id})[/dim]"
    )
    output.rule()

    # Save the decomposition alongside the mission for traceability.
    mp = mission.mission_paths(proj.id, mission_id)
    mp.root.mkdir(parents=True, exist_ok=True)
    (mp.root / "decomposition.json").write_text(decomp.model_dump_json(indent=2) + "\n")

    try:
        meta = asyncio.run(
            mission.dispatch(
                project=proj, specialist=r.specialist,
                ticket=task.description if task.description.strip() else ticket,
                roster_store=roster_store, project_store=project_store,
                worktree_manager=worktree_manager, limits=limits,
                on_message=_make_renderer(),
                mission_id=mission_id,
                manager_cost_usd=manager_cost,
                review=review, max_revisions=max_revisions,
            )
        )
    except KeyboardInterrupt:
        output.warn("interrupted")
        raise typer.Exit(code=130) from None
    except WorktreeError as e:
        output.die(str(e))
    output.rule()
    _print_summary(meta)
    if auto_merge:
        _run_auto_merge_single(proj, meta, target=merge_into)
    if meta.status is not MissionStatus.COMPLETED:
        raise typer.Exit(code=1)


def _dispatch_after_manager_parallel(
    proj: project_mod.Project,
    ticket: str,
    decomp: Decomposition,
    manager_cost: float,
    roster_store: RosterStore,
    project_store: project_mod.ProjectStore,
    worktree_manager: WorktreeManager,
    *,
    limits: RunLimits,
    skip_confirm: bool,
    auto_staff: bool,
    auto_merge: bool = False,
    merge_into: str | None = None,
    panels: bool = False,
    review: bool = False,
    max_revisions: int = 3,
) -> None:
    """Manager said parallel/sequential. Hand off to the parallel orchestrator."""
    confirm_cb: parallel.ConfirmCallback | None
    if skip_confirm:
        confirm_cb = lambda _d, _r: True  # noqa: E731
    else:
        confirm_cb = _confirm_decomposition

    task_ids = [t.id for t in decomp.tasks]
    use_panels = panels and cli_panels.stdout_is_tty()

    try:
        if use_panels:
            with cli_panels.PanelDisplay(task_ids) as panel_display:
                result = asyncio.run(
                    parallel.dispatch_parallel(
                        project=proj,
                        ticket=ticket,
                        roster_store=roster_store,
                        project_store=project_store,
                        worktree_manager=worktree_manager,
                        sub_mission_limits=limits,
                        make_sub_callback=panel_display.make_callback,
                        confirm=confirm_cb,
                        auto_staff=auto_staff,
                        decomposition_override=decomp,
                        review=review,
                        max_revisions=max_revisions,
                    )
                )
        else:
            result = asyncio.run(
                parallel.dispatch_parallel(
                    project=proj,
                    ticket=ticket,
                    roster_store=roster_store,
                    project_store=project_store,
                    worktree_manager=worktree_manager,
                    sub_mission_limits=limits,
                    make_sub_callback=_make_sub_renderer,
                    confirm=confirm_cb,
                    auto_staff=auto_staff,
                    decomposition_override=decomp,
                    review=review,
                    max_revisions=max_revisions,
                )
            )
    except KeyboardInterrupt:
        output.warn("interrupted")
        raise typer.Exit(code=130) from None
    except (ValidationError, ResolutionError, WorktreeError) as e:
        output.die(str(e))

    # Patch in the manager_cost we already paid (parallel.dispatch_parallel
    # records 0 because we passed decomposition_override).
    result.parent_meta.manager_cost_usd = manager_cost

    output.rule()
    _print_parallel_summary(result.parent_meta, result.sub_metas)
    _print_ownership_audit(result.parent_meta)
    _print_merge_plan(result.parent_meta, result.sub_metas)

    if auto_merge:
        _run_auto_merge_parallel(proj, result.parent_meta, result.sub_metas, target=merge_into)

    if result.parent_meta.status is not ParallelStatus.COMPLETED:
        raise typer.Exit(code=1)


_STAFFING_LABELS = {
    "already_assigned": "[dim]assigned[/dim]",
    "auto_assigned_from_roster": "[cyan]auto-assigned[/cyan]",
    "auto_hired_from_template": "[bold magenta]auto-hired[/bold magenta]",
    "fallback": "[yellow]fallback[/yellow]",
}


def _confirm_decomposition(
    decomp: Decomposition,
    resolved: list[tuple[str, str, str]],
) -> bool:
    """Print the decomposition and ask for y/N confirmation.

    `resolved` rows are (task_id, specialist_name, staffing_action).
    """
    output.rule("decomposition")
    output.info(f"[bold]kind:[/bold] {decomp.kind.value}    [dim]{decomp.rationale}[/dim]")
    if decomp.contract.needed:
        output.info(f"[bold]contract:[/bold] {decomp.contract.path}")
        output.info(f"[dim]{_truncate(decomp.contract.body, 200)}[/dim]")

    by_task = {tid: (name, action) for tid, name, action in resolved}
    new_hires = [name for _, name, action in resolved if action == "auto_hired_from_template"]
    auto_assigned = [name for _, name, action in resolved if action == "auto_assigned_from_roster"]

    table = Table(show_header=True, header_style="bold")
    table.add_column("task")
    table.add_column("specialist")
    table.add_column("staffing")
    table.add_column("owns", overflow="fold")
    table.add_column("depends_on")
    table.add_column("turns", justify="right")
    table.add_column("description", overflow="fold")
    for t in decomp.tasks:
        owns = ", ".join(t.owns_paths) if t.owns_paths else "[dim]-[/dim]"
        if t.excludes_paths:
            owns += " [dim](excl: " + ", ".join(t.excludes_paths) + ")[/dim]"
        deps = ", ".join(t.depends_on) if t.depends_on else "[dim]-[/dim]"
        spec_name, action = by_task.get(t.id, ("[red]?[/red]", "fallback"))
        staffing_label = _STAFFING_LABELS.get(action, action)
        if action == "auto_hired_from_template" and t.template_hint:
            staffing_label += f" [dim](← {t.template_hint})[/dim]"
        table.add_row(
            t.id,
            spec_name,
            staffing_label,
            owns,
            deps,
            str(t.estimated_turns),
            _truncate(t.description, 80),
        )
    output.print_table(table)

    if new_hires:
        output.info(
            f"[bold magenta]Will hire new specialist(s):[/bold magenta] {', '.join(new_hires)}"
        )
    if auto_assigned:
        output.info(
            f"[cyan]Will assign existing specialist(s) to this project:[/cyan] {', '.join(auto_assigned)}"
        )
    if decomp.merge_order:
        output.info(f"[dim]merge order: {' → '.join(decomp.merge_order)}[/dim]")
    output.rule()
    return typer.confirm("Proceed with this decomposition?", default=True)


def _make_sub_renderer(task_id: str) -> Any:
    """Per-sub-mission renderer that prefixes lines with [task_id]."""
    prefix = f"\\[[bold cyan]{task_id}[/bold cyan]] "

    def render(msg: Any) -> None:
        # Print prefix then delegate. Prefix is dim so the eye can group lines.
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    text = block.text.rstrip()
                    if text:
                        for line in text.splitlines():
                            output.info(f"{prefix}{line}")
                elif isinstance(block, ToolUseBlock):
                    args_preview = _truncate(_summarize_tool_args(block.name, block.input), 70)
                    output.info(f"{prefix}[dim]→ {block.name}({args_preview})[/dim]")
        elif isinstance(msg, UserMessage):
            if isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock) and block.is_error:
                        preview = _truncate(repr(block.content), 100)
                        output.warn(f"{prefix}  ← tool error: {preview}")
        elif isinstance(msg, ResultMessage):
            output.info(
                f"{prefix}[dim]turns={msg.num_turns} duration={msg.duration_ms}ms "
                f"cost=${(msg.total_cost_usd or 0):.4f}[/dim]"
            )

    return render


_PARALLEL_STATUS_STYLES = {
    ParallelStatus.PLANNED: "[dim]planned[/dim]",
    ParallelStatus.DISPATCHED: "[yellow]dispatched[/yellow]",
    ParallelStatus.COMPLETED: "[green]completed[/green]",
    ParallelStatus.PARTIAL: "[yellow]partial[/yellow]",
    ParallelStatus.FAILED: "[red]failed[/red]",
    ParallelStatus.CANCELLED: "[dim]cancelled[/dim]",
}


def _print_parallel_summary(parent: ParallelMissionMeta, subs: list[MissionMeta]) -> None:
    output.info(
        f"parent mission {parent.parent_mission_id}: "
        f"{_PARALLEL_STATUS_STYLES[parent.status]}"
    )
    output.info(f"  manager cost: ${parent.manager_cost_usd:.4f}")
    if not subs:
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("task")
    table.add_column("specialist")
    table.add_column("status")
    table.add_column("cost", justify="right")
    table.add_column("turns", justify="right")
    table.add_column("commits", justify="right")
    table.add_column("branch", overflow="fold")

    sub_by_id = {m.mission_id: m for m in subs}
    total_cost = parent.manager_cost_usd
    for ref in parent.sub_missions:
        m = sub_by_id.get(ref.mission_id)
        if m is None:
            continue
        total_cost += m.cost_usd
        table.add_row(
            ref.task_id,
            m.specialist,
            _STATUS_STYLES[m.status],
            f"${m.cost_usd:.4f}",
            str(m.turn_count),
            str(len(m.commits)),
            m.branch,
        )
    output.print_table(table)
    output.info(f"  total cost: ${total_cost:.4f}")


# ----- auto-merge -----------------------------------------------------------


def _run_auto_merge_single(
    proj: project_mod.Project,
    meta: MissionMeta,
    *,
    target: str | None = None,
) -> None:
    if meta.status is not MissionStatus.COMPLETED:
        output.warn("auto-merge skipped: mission did not complete cleanly")
        return
    plan = [parallel.MergeStep(
        task_id="single", branch=meta.branch,
        sub_mission_id=meta.mission_id, status=meta.status,
    )]
    _execute_auto_merge(proj, plan, target=target)


def _run_auto_merge_parallel(
    proj: project_mod.Project,
    parent: ParallelMissionMeta,
    subs: list[MissionMeta],
    *,
    target: str | None = None,
) -> None:
    if parent.status is not ParallelStatus.COMPLETED:
        output.warn(
            f"auto-merge skipped: parent status is {parent.status.value}; "
            "run the merge plan above manually for any branches you want to keep"
        )
        return
    plan = parallel.merge_plan(parent, subs)
    _execute_auto_merge(proj, plan, target=target)


def _execute_auto_merge(
    proj: project_mod.Project,
    plan: list[parallel.MergeStep],
    *,
    target: str | None,
) -> None:
    output.rule("auto-merge")
    repo = Path(proj.repo_path)
    if target is not None:
        output.info(f"merging into {target!r} branch in {proj.repo_path}...")
        try:
            results = parallel.auto_merge_into(repo, plan, target_branch=target)
        except parallel.MergePreflightError as e:
            output.fail(f"auto-merge preflight failed: {e}")
            return
    else:
        cur = parallel._current_branch(repo) or "HEAD"
        output.info(f"merging into current branch ({cur}) of {proj.repo_path}...")
        results = parallel.auto_merge(repo, plan)

    succeeded = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    for r in results:
        if r.success:
            output.success(f"{r.task_id:12s} {r.branch}  [dim]({r.detail})[/dim]")
        else:
            output.fail(f"{r.task_id:12s} {r.branch}  [dim]({r.detail})[/dim]")
    if failed:
        output.warn(
            f"auto-merge incomplete: {len(succeeded)} merged, {len(failed)} not merged. "
            "Resolve manually."
        )
    else:
        output.success(f"auto-merge: all {len(succeeded)} branch(es) merged")


def _print_ownership_audit(parent: ParallelMissionMeta) -> None:
    """If any sub wrote outside its declared lane, warn loudly before merge."""
    drifters = [(s.task_id, s.out_of_lane_files) for s in parent.sub_missions if s.out_of_lane_files]
    if not drifters:
        return
    output.rule("decomposition drift")
    output.warn(
        "Specialists wrote files outside their declared owns_paths. "
        "This is the most common cause of merge conflicts — review carefully:"
    )
    for task_id, files in drifters:
        preview = ", ".join(files[:5])
        more = f" (+{len(files) - 5} more)" if len(files) > 5 else ""
        output.warn(f"  [bold]{task_id}[/bold] wrote: {preview}{more}")
    output.info(
        "[dim]If two tasks wrote the same file, expect a conflict at merge time. "
        "Run `workforce mission show <parent-id>` for full details.[/dim]"
    )


def _print_merge_plan(parent: ParallelMissionMeta, subs: list[MissionMeta]) -> None:
    plan = merge_plan(parent, subs)
    if not plan:
        return
    output.rule("merge plan")
    completed = [s for s in plan if s.status is MissionStatus.COMPLETED]
    failed = [s for s in plan if s.status is not MissionStatus.COMPLETED]
    if completed:
        output.info("Run on the source repo, in this order:")
        for step in completed:
            output.info(f"  git merge --no-ff {step.branch}    [dim]# {step.task_id}[/dim]")
    if failed:
        output.warn("Skipped (sub-mission did not complete cleanly):")
        for step in failed:
            output.warn(f"  {step.branch}  [dim]({step.status})[/dim]")


def _print_summary(meta: mission.MissionMeta) -> None:
    style = {
        MissionStatus.COMPLETED: "[green]completed[/green]",
        MissionStatus.ERROR: "[red]error[/red]",
        MissionStatus.WALL_TIMEOUT: "[yellow]wall_timeout[/yellow]",
        MissionStatus.INTERRUPTED: "[yellow]interrupted[/yellow]",
        MissionStatus.TRAILER_VIOLATION: "[red]trailer_violation[/red]",
        MissionStatus.REVIEW_REJECTED: "[red]review_rejected[/red]",
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
    if meta.reviews:
        approved = meta.reviews[-1].approved
        verdict = "approved" if approved else "rejected"
        verdict_color = "green" if approved else "red"
        output.info(
            f"  review:    [{verdict_color}]{verdict}[/{verdict_color}] "
            f"after {len(meta.reviews)} round(s), revisions={meta.revision_rounds}, "
            f"review_cost=${meta.review_cost_usd:.4f}"
        )
        if not approved and meta.reviews[-1].issues:
            for issue in meta.reviews[-1].issues[:5]:
                output.warn(f"    • {issue}")
    if meta.error_detail:
        output.fail(f"  detail:    {meta.error_detail}")
    if meta.memory_delta_captured:
        output.info("  memory delta captured")
    output.info(
        f"  artifacts: {paths.project_dir(meta.project_id) / 'missions' / meta.mission_id}"
    )


# ----- mission lookup -------------------------------------------------------


def _load_any_meta(
    project_id: str, mission_id: str
) -> MissionMeta | ParallelMissionMeta | None:
    """Load either a single-mission meta or a parent (parallel) meta."""
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
    """Locate a mission (single, parent, or sub) across all registered projects."""
    pstore = project_mod.ProjectStore()
    for proj in pstore.list():
        meta = _load_any_meta(proj.id, mission_id)
        if meta is not None:
            return proj, meta
    output.die(f"no mission with id {mission_id!r} found in any project")
    raise AssertionError("unreachable")  # pragma: no cover


def _list_project_missions(
    project_id: str,
) -> list[MissionMeta | ParallelMissionMeta]:
    """Return all mission metas (singles, parents, subs) for a project."""
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


# ----- missions list (top-level) --------------------------------------------


_STATUS_STYLES = {
    MissionStatus.COMPLETED: "[green]completed[/green]",
    MissionStatus.ERROR: "[red]error[/red]",
    MissionStatus.WALL_TIMEOUT: "[yellow]wall_timeout[/yellow]",
    MissionStatus.INTERRUPTED: "[yellow]interrupted[/yellow]",
    MissionStatus.TRAILER_VIOLATION: "[red]trailer_violation[/red]",
    MissionStatus.REVIEW_REJECTED: "[red]review_rejected[/red]",
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


def _render_replay_event(evt: dict[str, Any], *, show_thinking: bool) -> None:
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


# ----- mission sub-typer (show, clean, prune) -------------------------------


mission_sub = typer.Typer(
    name="mission",
    help="Inspect and clean up individual missions.",
    no_args_is_help=True,
)


@mission_sub.command("show")
def mission_show(mission_id: str = typer.Argument(..., help="Mission id.")) -> None:
    """Show one mission's details. Works for single, parent (parallel), or sub missions."""
    paths.ensure_layout()
    proj, meta = _find_mission(mission_id)
    if isinstance(meta, ParallelMissionMeta):
        _show_parent_meta(proj, meta)
    else:
        _show_single_meta(proj, meta)


def _show_single_meta(proj: project_mod.Project, meta: MissionMeta) -> None:
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


@mission_sub.command("tail")
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
    proj, meta = _find_mission(mission_id)
    mp = mission.mission_paths(proj.id, mission_id)
    if not mp.events.is_file():
        if isinstance(meta, ParallelMissionMeta):
            output.die(
                f"{mission_id} is a parent mission — tail each sub-mission individually:\n  "
                + "\n  ".join(f"workforce mission tail {s.mission_id}" for s in meta.sub_missions)
            )
        output.die(f"no events log at {mp.events}")

    label = (
        meta.specialist if isinstance(meta, MissionMeta)
        else f"({meta.decomposition_kind.value} parent)"
    )
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
    if isinstance(meta, ParallelMissionMeta):
        output.die(
            f"{mission_id} is a parent mission; clean each sub-mission instead:\n  "
            + "\n  ".join(f"workforce mission clean {s.mission_id}" for s in meta.sub_missions)
        )
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


# ----- branches sub-typer ---------------------------------------------------


branches_sub = typer.Typer(
    name="branches",
    help="Inspect and clean up workforce/* branches in a project.",
    no_args_is_help=True,
)


@branches_sub.command("prune")
def branches_prune(
    project_ref: str = typer.Argument(..., help="Project name or id.", metavar="PROJECT"),
    into: str | None = typer.Option(
        None, "--into",
        metavar="BRANCH",
        help="Compare merge status against this branch. Default: the project's current branch.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List what would be deleted without changing anything."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Delete merged workforce/* branches (and their worktrees) from a project."""
    paths.ensure_layout()
    pstore = project_mod.ProjectStore()
    try:
        proj = pstore.resolve(project_ref)
    except project_mod.ProjectError as e:
        output.die(str(e))

    repo = Path(proj.repo_path)
    if not repo.is_dir():
        output.die(f"project {proj.name!r} repo path missing: {repo}")

    target = into
    if target is None:
        # Default: current branch of the source repo.
        target = parallel._current_branch(repo)
        if target is None:
            output.die(
                "could not determine current branch (detached HEAD?). "
                "Pass --into BRANCH explicitly."
            )

    try:
        merged = find_workforce_branches(repo, merged_into=target)
    except subprocess.CalledProcessError as e:
        output.die(f"git failed: {e}")

    if not merged:
        output.info(
            f"no merged workforce/* branches in {proj.name} "
            f"(checked against {target!r})"
        )
        return

    output.info(f"merged into {target!r} and ready to delete:")
    for b in merged:
        output.info(f"  {b}")

    if dry_run:
        output.info("[dim](dry-run — nothing changed)[/dim]")
        return

    if not yes:
        confirm = typer.confirm(
            f"Delete {len(merged)} branch(es) and their worktrees?", default=False
        )
        if not confirm:
            output.info("aborted")
            raise typer.Exit()

    wm = WorktreeManager()
    deleted: list[str] = []
    skipped: list[tuple[str, str]] = []

    for branch in merged:
        # Find any worktree currently holding this branch and remove it first.
        for entry in wm.list_git_worktrees(repo):
            if entry.branch == f"refs/heads/{branch}":
                try:
                    wm.remove(repo, entry.path, force=True)
                except Exception as e:
                    skipped.append((branch, f"worktree removal failed: {e}"))
                    break
        else:
            # No matching worktree — proceed straight to branch deletion.
            pass

        # If we hit a worktree-removal error above, we should have continued
        # to the next branch. Use a sentinel by checking skipped.
        if skipped and skipped[-1][0] == branch:
            continue

        try:
            r = subprocess.run(
                ["git", "branch", "-d", branch],
                cwd=repo, capture_output=True, text=True, check=False,
            )
        except OSError as e:
            skipped.append((branch, f"git invoke failed: {e}"))
            continue
        if r.returncode == 0:
            deleted.append(branch)
        else:
            err = (r.stderr.strip() or r.stdout.strip())[:200]
            skipped.append((branch, err))

    for b in deleted:
        output.success(f"deleted {b}")
    for b, why in skipped:
        output.warn(f"skipped {b}: {why}")
    output.info(f"pruned {len(deleted)} of {len(merged)} branch(es)")


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
    threshold = dt.datetime.now(dt.UTC) - _parse_duration(older_than)

    pstore = project_mod.ProjectStore()
    wm = WorktreeManager()
    candidates: list[tuple[project_mod.Project, MissionMeta]] = []
    for proj in pstore.list():
        for meta in _list_project_missions(proj.id):
            # Parent metas have no worktree of their own; their subs do.
            if not isinstance(meta, MissionMeta):
                continue
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
