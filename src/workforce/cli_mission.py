"""CLI commands for missions: dispatch, list, show, replay, clean, prune."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass
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
    window: bool = typer.Option(
        False, "--window",
        help=(
            "Background the mission and pop up a separate terminal window "
            "streaming its output. Returns the mission id immediately. "
            "Requires --specialist (single-mission only)."
        ),
    ),
    background: bool = typer.Option(
        False, "--background",
        help=(
            "Background the mission and return immediately, without opening "
            "a terminal window. Used by the Manager session — the shared "
            "`workforce project tail` window picks up the new mission "
            "automatically. Requires --specialist."
        ),
    ),
    mission_id_override: str | None = typer.Option(
        None, "--mission-id", hidden=True,
        help="Internal: pre-allocated mission id used by --window/--background forks.",
    ),
) -> None:
    """Dispatch a mission. The Manager plans it, then it runs.

    The Manager runs first to decide whether the ticket should fan out across
    multiple specialists in parallel, run as a sequential chain, or just go
    to one specialist. Pass --specialist to skip the Manager and dispatch
    directly to a named specialist (cheaper for tiny tickets).
    """
    if window and background:
        output.die("--window and --background are mutually exclusive")
    if window or background:
        _dispatch_detached(
            project_ref=project_ref, ticket=ticket, specialist=specialist,
            mission_id_override=mission_id_override,
            max_turns=max_turns, max_cost=max_cost, max_wall=max_wall,
            review=review, max_revisions=max_revisions,
            open_window=window,
        )
        return

    roster_store, project_store, worktree_manager = _stores()

    try:
        proj = project_store.resolve(project_ref)
    except project_mod.ProjectError as e:
        output.die(str(e))

    repo_path = Path(proj.repo_path)
    if not repo_path.is_dir():
        label = "workspace" if proj.kind == "workspace" else "repo"
        output.die(
            f"project {proj.name!r} points at {proj.repo_path}, which is missing. "
            f"Did you move the {label}?"
        )

    # Preflight: the source repo needs at least one commit and a clean tree.
    # Workspace projects skip both — they have no git state.
    if proj.kind == "repo":
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

    # Workspace projects don't support the git-flavored flags. Reject them
    # loudly here so the user doesn't discover the limitation mid-mission.
    if proj.kind == "workspace":
        if auto_merge or merge_into is not None:
            output.die(
                "--auto-merge / --merge-into don't apply to workspace projects "
                "(no branches to merge)."
            )
        if review:
            output.die(
                "workspace projects don't support --review yet (the Reviewer is "
                "git-only, it diffs the worktree against base_sha)."
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
            mission_id=mission_id_override,
        )
        return

    # Manager-driven dispatch may decide to fan out across specialists in
    # parallel. For workspace projects, parallel sub-missions all share the
    # project directory (no worktree isolation) — safety comes from the
    # Manager validating non-overlapping `owns_paths` at plan time and the
    # `can_use_tool` callback enforcing those lanes at write time.
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
    mission_id: str | None = None,
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
                mission_id=mission_id,
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


def _dispatch_detached(
    *,
    project_ref: str,
    ticket: str,
    specialist: str | None,
    mission_id_override: str | None,
    max_turns: int,
    max_cost: float,
    max_wall: float,
    review: bool,
    max_revisions: int,
    open_window: bool,
) -> None:
    """`--window` / `--background` shared path.

    Pre-allocates the mission id, forks a detached `workforce dispatch ...
    --mission-id <id>` subprocess that runs the actual mission, and prints
    the id immediately. If `open_window` is True, also pops up a terminal
    tailing this single mission's output (one window per dispatch).
    For `--background` (open_window=False) the caller is expected to have
    a `workforce project tail` window already open — the Manager session
    runs one such shared window and uses --background for every dispatch.
    """
    flag = "--window" if open_window else "--background"
    if specialist is None:
        output.die(
            f"{flag} requires --specialist (single-mission only). "
            "For multi-mission dispatch from the Manager, use `workforce manage`."
        )

    mission_id = mission_id_override or mission.generate_mission_id()

    # Re-invoke ourselves without the detach flag, with the pinned mission id.
    # `python -m workforce` runs the same package the parent runs regardless
    # of pip/pipx install path.
    argv: list[str] = [
        sys.executable, "-m", "workforce", "dispatch", project_ref, ticket,
        "--specialist", specialist,
        "--mission-id", mission_id,
        "--max-turns", str(max_turns),
        "--max-cost", str(max_cost),
        "--max-wall", str(max_wall),
    ]
    if review:
        argv += ["--review", "--max-revisions", str(max_revisions)]

    # Detach so the child survives this process exiting. stdout/stderr go to
    # /dev/null — the tail window renders from events.jsonl, not stdout.
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        output.die(f"could not spawn background dispatch: {e}")

    output.success(f"dispatched mission {mission_id}")

    if open_window:
        from workforce.terminal import open_terminal_window
        spawned = open_terminal_window(
            title=f"workforce: {mission_id}",
            command=["workforce", "mission", "tail", mission_id, "--show-thinking"],
        )
        if spawned:
            output.info("[dim]live output is in the new terminal window.[/dim]")
        else:
            output.info(
                "could not open a terminal window — watch with: "
                f"[bold]workforce mission tail {mission_id}[/bold]"
            )


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
    """Manager said parallel/sequential. Confirm-loop here, then orchestrate."""
    parent_mission_id = mission.generate_mission_id()

    # Confirm loop: validate → resolve for display → ask y/N/d → maybe replan → repeat.
    while True:
        try:
            manager.validate_decomposition(
                decomp,
                repo_path=Path(proj.repo_path),
                available_specialists=list(proj.assigned_specialists) or None,
            )
        except ValidationError as e:
            output.die(f"Manager produced an invalid decomposition: {e}")

        try:
            resolved = parallel.resolve_task_specialists(
                decomp,
                parent_mission_id=parent_mission_id,
                project=proj,
                roster_store=roster_store,
                project_store=project_store,
                auto_staff=auto_staff,
            )
        except ResolutionError as e:
            output.die(str(e))

        if skip_confirm:
            break

        rows = [(r.task.id, r.specialist.name, r.staffing_action) for r in resolved]
        decision = _confirm_decomposition(decomp, rows)
        if decision.action == "cancel":
            output.info("aborted")
            return
        if decision.action == "proceed":
            break
        # discuss: re-run Manager with the user's feedback as context.
        output.info("[dim]Manager replanning with your feedback...[/dim]")
        specs_info = parallel._build_specialist_info(proj, roster_store, project_store)
        try:
            decomp, replan_cost, _ = asyncio.run(manager.run_manager(
                ticket=ticket,
                repo_path=Path(proj.repo_path),
                project_specialists=specs_info,
                prior_decomposition=decomp,
                user_feedback=decision.feedback,
            ))
        except ManagerError as e:
            output.die(f"manager replan failed: {e}")
        manager_cost += replan_cost
        output.info(
            f"[dim]manager: kind={decomp.kind.value} +cost=${replan_cost:.4f} "
            f"(total: ${manager_cost:.4f}) — {decomp.rationale}[/dim]"
        )
        # If the Manager replanned to a single-task decomposition, switch
        # to the single-task path; we shouldn't ask the user to confirm a
        # one-row table.
        if decomp.kind is DecompositionKind.SINGLE:
            output.info("[dim](Manager dropped to single-task; dispatching as one mission)[/dim]")
            _dispatch_after_manager_single(
                proj, ticket, decomp, manager_cost,
                roster_store, project_store, worktree_manager,
                limits=limits, auto_staff=auto_staff,
                auto_merge=auto_merge, merge_into=merge_into,
                review=review, max_revisions=max_revisions,
            )
            return
        # Loop back: validate + resolve + confirm the new plan.

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
                        confirm=None,  # already confirmed in the loop above
                        auto_staff=auto_staff,
                        decomposition_override=decomp,
                        parent_mission_id=parent_mission_id,
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
                    confirm=None,
                    auto_staff=auto_staff,
                    decomposition_override=decomp,
                    parent_mission_id=parent_mission_id,
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


# What the confirm callback returns. "discuss" means the user wants to
# replan; the .feedback string is what they typed.
@dataclass
class ConfirmDecision:
    action: str  # "proceed" | "cancel" | "discuss"
    feedback: str = ""


def _confirm_decomposition(
    decomp: Decomposition,
    resolved: list[tuple[str, str, str]],
) -> ConfirmDecision:
    """Print the decomposition and ask for y/N/d.

    `resolved` rows are (task_id, specialist_name, staffing_action).
    Returns a ConfirmDecision telling the caller what to do next.
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

    while True:
        choice = typer.prompt(
            "Proceed? [y]es / [n]o / [d]iscuss with Manager",
            default="y",
        ).strip().lower()
        if choice in {"y", "yes"}:
            return ConfirmDecision(action="proceed")
        if choice in {"n", "no"}:
            return ConfirmDecision(action="cancel")
        if choice in {"d", "discuss"}:
            output.info(
                "[dim]Tell the Manager what to change "
                "(e.g. \"split tester into unit + e2e\", \"don't use Tailwind\"). "
                "Empty input cancels.[/dim]"
            )
            feedback = typer.prompt("> ", default="").strip()
            if not feedback:
                output.info("(no feedback given; staying on this decomposition)")
                continue
            return ConfirmDecision(action="discuss", feedback=feedback)
        output.warn(f"unknown choice {choice!r}; pick y, n, or d")


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
    # Workspace missions have no branch; the CLI rejects --auto-merge for them
    # before we get here, but assert for type narrowing.
    assert meta.branch is not None, "auto-merge requires a repo-kind mission"
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
        # Show a guided fix path for the first failure that had conflicting files.
        # Subsequent failures are usually "skipped after earlier failure" — no
        # need to repeat the same advice for them.
        first_with_conflicts = next(
            (r for r in failed if r.conflicting_files), None
        )
        if first_with_conflicts is not None:
            _print_conflict_help(proj, first_with_conflicts)
    else:
        output.success(f"auto-merge: all {len(succeeded)} branch(es) merged")


def _print_conflict_help(
    proj: project_mod.Project,
    failed: parallel.AutoMergeStepResult,
) -> None:
    """Tell the user exactly what to do with the conflicting files."""
    output.rule("how to resolve")
    output.info(
        f"branch [bold]{failed.branch}[/bold] conflicts with the target on:"
    )
    for f in failed.conflicting_files[:10]:
        output.info(f"  {f}")
    if len(failed.conflicting_files) > 10:
        output.info(f"  [dim](+{len(failed.conflicting_files) - 10} more)[/dim]")
    output.info("")
    output.info("To resolve, from your project repo:")
    output.info(f"  [dim]cd {proj.repo_path}[/dim]")
    output.info(f"  git merge --no-ff {failed.branch}")
    output.info("    [dim]then for each conflicting file:[/dim]")
    output.info("      [dim]# pick one of:[/dim]")
    output.info("      git checkout --ours <file>     [dim]# keep target's version[/dim]")
    output.info(f"      git checkout --theirs <file>   [dim]# keep {failed.branch}'s version[/dim]")
    output.info("      $EDITOR <file>                 [dim]# merge by hand[/dim]")
    output.info("    git add <file>")
    output.info("  git commit --no-edit")
    output.info("[dim]Or abort entirely: git merge --abort[/dim]")


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
    if meta.branch is None:
        # Workspace mission — no branch, no commits, just a working dir.
        output.info(f"  workspace: {meta.worktree_path}")
    else:
        output.info(f"  branch:    {meta.branch}")
        output.info(f"  worktree:  {meta.worktree_path}")
    output.info(f"  duration:  {meta.duration_seconds:.1f}s")
    output.info(f"  cost:      ${meta.cost_usd:.4f}")
    output.info(f"  turns:     {meta.turn_count}")
    if meta.branch is not None:
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
    if meta.branch is None:
        # Workspace mission — no worktree was ever created. Nothing to clean.
        output.info(
            f"workspace mission — nothing to clean (mission ran in {meta.worktree_path})"
        )
        return
    # Repo missions always populate worktree_path; this is a tautology for type
    # narrowing.
    assert meta.worktree_path is not None
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
            # Workspace missions have no worktree to remove.
            if meta.branch is None or meta.worktree_path is None:
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
        # Workspace missions are filtered out when building `candidates`; this
        # assert is just for type narrowing.
        assert meta.worktree_path is not None
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
