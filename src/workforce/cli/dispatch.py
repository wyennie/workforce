"""`workforce dispatch` and all its helpers.

The Manager planning, single-vs-parallel branching, decomposition confirmation,
detached `--window`/`--background` forks, and the post-run summaries all live
here.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import typer
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from rich.table import Table

from workforce import (
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
)
from workforce.runner import RunLimits
from workforce.specialist import RosterStore
from workforce.worktree import (
    WorktreeError,
    WorktreeManager,
    ensure_branch,
    has_commits,
    is_repo_clean,
)

from . import panels as panels_mod
from ._common import (
    _PARALLEL_STATUS_STYLES,
    _STATUS_STYLES,
    _make_renderer,
    _stores,
    _summarize_tool_args,
    _truncate,
)
from .merge import (
    _print_merge_plan,
    _print_ownership_audit,
    _run_auto_merge_parallel,
    _run_auto_merge_single,
)


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
    branch: str | None = typer.Option(
        None,
        "--branch",
        metavar="BRANCH",
        help=(
            "Staging branch for this dispatch. Mission worktrees fork from "
            "BRANCH (created from current HEAD if missing), and on success the "
            "work is auto-merged back into BRANCH. main is never touched. "
            "Implies --auto-merge --merge-into BRANCH; mutually exclusive with "
            "--merge-into."
        ),
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
    if branch is not None and merge_into is not None:
        output.die("--branch and --merge-into are mutually exclusive (--branch sets the merge target)")
    if window or background:
        _dispatch_detached(
            project_ref=project_ref, ticket=ticket, specialist=specialist,
            mission_id_override=mission_id_override,
            max_turns=max_turns, max_cost=max_cost, max_wall=max_wall,
            review=review, max_revisions=max_revisions,
            open_window=window,
            branch=branch,
            auto_merge=auto_merge,
            merge_into=merge_into,
            auto_staff=auto_staff,
            panels=panels,
            yes=yes,
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
        if auto_merge or merge_into is not None or branch is not None:
            output.die(
                "--auto-merge / --merge-into / --branch don't apply to workspace "
                "projects (no branches to merge)."
            )
        if review:
            output.die(
                "workspace projects don't support --review yet (the Reviewer is "
                "git-only, it diffs the worktree against base_sha)."
            )

    # --branch BRANCH: ensure BRANCH exists (create from HEAD if missing), then
    # behave as if --merge-into BRANCH was passed. Worktrees will fork from
    # BRANCH's tip rather than current HEAD.
    if branch is not None:
        try:
            ensure_branch(repo_path, branch)
        except WorktreeError as e:
            output.die(str(e))
        merge_into = branch
        auto_merge = True

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
            base_branch=branch,
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
        base_branch=branch,
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
    base_branch: str | None = None,
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
                start_point=base_branch,
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
    branch: str | None = None,
    auto_merge: bool = False,
    merge_into: str | None = None,
    auto_staff: bool = True,
    panels: bool = False,
    yes: bool = False,
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
    if branch is not None:
        argv += ["--branch", branch]
    if auto_merge:
        argv += ["--auto-merge"]
    if merge_into is not None:
        argv += ["--merge-into", merge_into]
    if not auto_staff:
        argv += ["--no-auto-staff"]
    if panels:
        argv += ["--panels"]
    if yes:
        argv += ["--yes"]

    # Pre-create the mission artifacts directory and redirect child stderr to
    # startup.log so any early crash (bad flag, import error) is diagnosable.
    startup_log_fh = None
    meta_path: Path | None = None
    try:
        _ps = project_mod.ProjectStore()
        _proj = _ps.resolve(project_ref)
        mp = mission.mission_paths(_proj.id, mission_id)
        mp.root.mkdir(parents=True, exist_ok=True)
        startup_log_path = mp.root / "startup.log"
        startup_log_fh = open(startup_log_path, "w")  # noqa: WPS515
        meta_path = mp.meta
    except Exception:
        # Non-fatal: fall back to /dev/null if we can't set up the log file.
        pass

    # Detach so the child survives this process exiting. stdout goes to
    # /dev/null; stderr goes to startup.log (or /dev/null as fallback) so any
    # startup crash is diagnosable without a terminal.
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=startup_log_fh if startup_log_fh is not None else subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        if startup_log_fh is not None:
            startup_log_fh.close()
        output.die(f"could not spawn background dispatch: {e}")
    finally:
        if startup_log_fh is not None:
            startup_log_fh.close()

    output.success(f"dispatched mission {mission_id}")

    # Poll briefly for meta.json to confirm the child started correctly.
    # If it never appears but startup.log has content, warn the user immediately
    # rather than making them discover the failure via `mission show`.
    if meta_path is not None:
        import time as _time
        for _ in range(12):  # up to 3 seconds (12 × 0.25s)
            _time.sleep(0.25)
            if meta_path.is_file():
                break
        else:
            # meta.json never appeared — child may have crashed.
            try:
                log_content = startup_log_path.read_text().strip()
            except Exception:
                log_content = ""
            if log_content:
                output.warn(
                    f"[yellow]Mission {mission_id} may have failed to start. "
                    f"startup.log:[/yellow]\n{log_content[:500]}"
                )

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


# ----- Manager-driven dispatch ----------------------------------------------


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
    base_branch: str | None = None,
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
                on_message=_make_manager_renderer(),
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
            base_branch=base_branch,
        )
    else:
        _dispatch_after_manager_parallel(
            proj, ticket, decomp, manager_cost,
            roster_store, project_store, worktree_manager,
            limits=limits, skip_confirm=skip_confirm, auto_staff=auto_staff,
            auto_merge=auto_merge, merge_into=merge_into,
            panels=panels,
            review=review, max_revisions=max_revisions,
            base_branch=base_branch,
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
    base_branch: str | None = None,
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
                start_point=base_branch,
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
    base_branch: str | None = None,
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
                on_message=_make_manager_renderer(),
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
                base_branch=base_branch,
            )
            return
        # Loop back: validate + resolve + confirm the new plan.

    task_ids = [t.id for t in decomp.tasks]
    use_panels = panels and panels_mod.stdout_is_tty()

    try:
        if use_panels:
            with panels_mod.PanelDisplay(task_ids) as panel_display:
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
                        base_branch=base_branch,
                        manager_cost_usd=manager_cost,
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
                    base_branch=base_branch,
                    manager_cost_usd=manager_cost,
                )
            )
    except KeyboardInterrupt:
        output.warn("interrupted")
        raise typer.Exit(code=130) from None
    except (ValidationError, ResolutionError, WorktreeError) as e:
        output.die(str(e))

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
    action: Literal["proceed", "cancel", "discuss"]
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
    """Return an ``on_message`` callback that prefixes every output line with ``[task_id]``.

    Used in non-panels parallel mode so the interleaved stream remains readable.
    """
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


def _make_manager_renderer() -> Any:
    """Return an ``on_message`` callback for Manager planning turns.

    Renders tool calls as ``[dim]→ Tool(arg)[/dim]`` lines so the user can see
    the Manager inspecting the repo. Text output is suppressed — the Manager's
    final reply is a JSON blob, not prose for users.
    """

    def render(msg: Any) -> None:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    args_preview = _truncate(_summarize_tool_args(block.name, block.input), 80)
                    output.info(f"[dim]→ {block.name}({args_preview})[/dim]")
                # Text blocks are intentionally skipped — the Manager's prose
                # output is raw JSON; rendering it would confuse users.

    return render


def _print_parallel_summary(parent: ParallelMissionMeta, subs: list[MissionMeta]) -> None:
    """Print the per-task status table and total cost for a parallel mission."""
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


def _print_summary(meta: mission.MissionMeta) -> None:
    """Print a single-mission status block (branch, cost, turns, review verdict)."""
    output.info(f"mission {meta.mission_id}: {_STATUS_STYLES[meta.status]}")
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
