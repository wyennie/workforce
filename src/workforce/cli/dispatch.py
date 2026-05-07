"""`workforce dispatch` and all its helpers.

The Manager planning, single-vs-parallel branching, decomposition confirmation,
detached `--window`/`--background` forks, and the post-run summaries all live
here.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import typer
from click import ParameterSource
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from rich import box
from rich.panel import Panel
from rich.table import Table

from workforce import (
    github as github_mod,
    manager,
    mission,
    output,
    parallel,
    paths,
)
from workforce.config import load_global_config
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
from ._completions import complete_project, complete_specialist
from ._common import (
    _PARALLEL_STATUS_BADGES,
    _PARALLEL_STATUS_STYLES,
    _STATUS_BADGES,
    _STATUS_STYLES,
    _make_renderer,
    _relative_time,
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


# ----- Ticket resolution helpers ---------------------------------------------

_TICKET_TEMPLATE = """\
# workforce ticket
# ----------------
# Describe the work to be done below.
# Lines starting with '#' will be stripped before the ticket is submitted.
# Save and close the editor to continue. Leave the file blank to abort.
#
"""


def _read_ticket(
    ticket: str | None,
    file: str | None,
    stdin: bool,
) -> str:
    """Resolve ticket text from a positional arg, --file, --stdin, or $EDITOR.

    Exactly one source may be active at a time. Falls back to $EDITOR when
    none of the explicit sources are provided.

    Args:
        ticket: Positional ticket argument (or None).
        file: Path to a file containing the ticket (or None).
        stdin: If True, read ticket from sys.stdin.

    Returns:
        Non-empty ticket text.
    """
    n_sources = (ticket is not None) + (file is not None) + stdin
    if n_sources > 1:
        output.die(
            "conflicting ticket sources: use at most one of the positional "
            "ticket argument, --file, or --stdin"
        )
    if ticket is not None:
        if not ticket.strip():
            output.die("ticket text is empty")
        return ticket
    if file is not None:
        try:
            text = Path(file).read_text()
        except OSError as e:
            output.die(f"could not read --file {file!r}: {e}")
        if not text.strip():
            output.die(f"--file {file!r} is empty")
        return text
    if stdin:
        text = sys.stdin.read()
        if not text.strip():
            output.die("stdin input is empty")
        return text
    # Fall back to $EDITOR. In CI mode this is an error since there's no TTY.
    if output.is_ci_mode():
        output.die(
            "no ticket provided; pass the ticket text as a positional argument, "
            "via --file PATH, or via --stdin"
        )
    return _read_ticket_from_editor()


def _read_ticket_from_editor() -> str:
    """Open $EDITOR with a ticket template; strip comment lines and return content."""
    editor = os.environ.get("EDITOR", "nano")
    fd, tmp_path_str = tempfile.mkstemp(suffix=".md", prefix="workforce-ticket-")
    tmp_path = Path(tmp_path_str)
    try:
        os.close(fd)
        tmp_path.write_text(_TICKET_TEMPLATE)
        result = subprocess.run([editor, str(tmp_path)])
        if result.returncode != 0:
            output.die(f"editor {editor!r} exited with code {result.returncode}")
        raw = tmp_path.read_text()
    finally:
        tmp_path.unlink(missing_ok=True)

    lines = [ln for ln in raw.splitlines() if not ln.startswith("#")]
    text = "\n".join(lines).strip()
    if not text:
        output.die("ticket is empty (save the editor with content to continue)")
    return text


# ----- CI summary helpers ----------------------------------------------------


def _ci_summary_single(meta: MissionMeta) -> dict:
    """Build the CI JSON summary dict from a completed single MissionMeta."""
    return {
        "mission_id": meta.mission_id,
        "status": meta.status.value,
        "cost_usd": round(
            meta.cost_usd + meta.manager_cost_usd + meta.review_cost_usd, 6
        ),
        "branch": meta.branch,
        "commits": len(meta.commits),
    }


def _ci_summary_parallel(
    parent: ParallelMissionMeta,
    subs: list[MissionMeta],
) -> dict:
    """Build the CI JSON summary dict from a parallel mission result."""
    total_cost = parent.manager_cost_usd + sum(
        m.cost_usd + m.review_cost_usd for m in subs
    )
    total_commits = sum(len(m.commits) for m in subs)
    return {
        "mission_id": parent.parent_mission_id,
        "status": parent.status.value,
        "cost_usd": round(total_cost, 6),
        "branch": None,
        "commits": total_commits,
    }


def _write_ci_summary(summary: dict, output_file: Path | None) -> None:
    """Print the CI JSON summary to stdout and optionally to a file."""
    text = json.dumps(summary)
    print(text, flush=True)
    if output_file is not None:
        output_file.write_text(text + "\n")


# ----- Dry-run helpers -------------------------------------------------------


def _print_dry_run_direct(
    specialist_name: str,
    ticket: str,
    limits: RunLimits,
    roster_store: RosterStore,
) -> None:
    """Print dry-run summary for a direct (--specialist bypass) dispatch."""
    output.rule("dry-run")
    output.info(f"[bold]specialist:[/bold] {specialist_name}")
    output.info(f"[bold]ticket:[/bold]     {_truncate(ticket, 80)}")
    output.info(
        f"[bold]limits:[/bold]     turns={limits.max_turns}  "
        f"cost=${limits.max_budget_usd:.2f}  wall={limits.max_wall_seconds:.0f}s"
    )
    stats = roster_store.load_stats(specialist_name)
    total_missions = stats.missions_completed + stats.missions_failed
    if total_missions > 0:
        avg_cost = stats.total_cost_usd / total_missions
        output.info(
            f"[bold]est. cost:[/bold]  ${avg_cost:.4f} "
            f"([dim]avg over {total_missions} missions[/dim])"
        )
    else:
        output.info(
            "[bold]est. cost:[/bold]  [dim]no data — first mission for this specialist[/dim]"
        )
    output.rule()


def _print_dry_run_manager(
    decomp: Decomposition,
    resolved: list[tuple[str, str, str]],
    roster_store: RosterStore,
) -> None:
    """Print dry-run summary for a manager-planned dispatch."""
    output.rule("dry-run: decomposition")
    output.info(f"[bold]kind:[/bold] {decomp.kind.value}    [dim]{decomp.rationale}[/dim]")

    by_task = {tid: (name, action) for tid, name, action in resolved}
    table = Table(show_header=True, header_style="bold")
    table.add_column("task")
    table.add_column("specialist")
    table.add_column("owns", overflow="fold")
    table.add_column("depends_on")
    table.add_column("turns", justify="right")
    table.add_column("description", overflow="fold")
    for t in decomp.tasks:
        owns = ", ".join(t.owns_paths) if t.owns_paths else "[dim]-[/dim]"
        deps = ", ".join(t.depends_on) if t.depends_on else "[dim]-[/dim]"
        spec_name, _ = by_task.get(t.id, ("[red]?[/red]", ""))
        table.add_row(
            t.id, spec_name, owns, deps,
            str(t.estimated_turns), _truncate(t.description, 60),
        )
    output.print_table(table)

    # Estimated cost: sum of per-specialist averages.
    spec_names = list({name for _, name, _ in resolved})
    est_total: float | None = None
    for spec_name in spec_names:
        stats = roster_store.load_stats(spec_name)
        total_m = stats.missions_completed + stats.missions_failed
        if total_m > 0:
            avg = stats.total_cost_usd / total_m
            est_total = (est_total or 0.0) + avg
    if est_total is not None:
        output.info(f"[bold]est. total cost:[/bold] ${est_total:.4f}")
    else:
        output.info("[bold]est. total cost:[/bold] [dim]no data[/dim]")
    output.rule()


# ----- Specialist onboarding wizard ------------------------------------------


def _onboard_specialist(
    specialist: str,
    proj: project_mod.Project,
    roster_store: RosterStore,
    project_store: project_mod.ProjectStore,
    *,
    skip_prompts: bool,
) -> project_mod.Project:
    """Ensure *specialist* exists and is assigned to *proj*.

    In non-interactive mode (``skip_prompts=True``), dies with a clear message
    when either condition is not met. In interactive mode, offers to hire from
    a template and/or assign to the project.

    Args:
        specialist: Name of the specialist to check.
        proj: The project the specialist must be assigned to.
        roster_store: Roster store used to look up / save specialists.
        project_store: Project store used to save assignment changes.
        skip_prompts: When True, die instead of prompting.

    Returns:
        The (possibly updated) Project after any assignment changes.
    """
    # Step 1: ensure specialist exists in the roster.
    if not roster_store.exists(specialist):
        if skip_prompts:
            output.die(f"no such specialist: {specialist!r}")
        templates_list = "/".join(sorted(specialist_mod.TEMPLATES))
        choice = typer.prompt(
            f"Specialist {specialist!r} doesn't exist. "
            f"Hire from template? [{templates_list}/skip]",
            default="skip",
        ).strip().lower()
        if choice in specialist_mod.TEMPLATES:
            new_spec = specialist_mod.Specialist.from_template(specialist, choice)
            roster_store.save(new_spec)
            output.success(f"hired {specialist!r} from template {choice!r}")
        else:
            output.die(f"no such specialist: {specialist!r}")

    # Step 2: ensure specialist is assigned to this project.
    if specialist not in proj.assigned_specialists:
        if skip_prompts:
            output.die(
                f"{specialist!r} isn't assigned to {proj.name}. "
                f"Run `workforce project assign {proj.name} {specialist}` first."
            )
        answer = typer.confirm(
            f"Specialist {specialist!r} isn't assigned to {proj.name!r}. Assign now?",
            default=True,
        )
        if answer:
            updated = proj.model_copy(
                update={
                    "assigned_specialists": list(proj.assigned_specialists) + [specialist]
                }
            )
            project_store.save(updated, overwrite=True)
            output.success(f"assigned {specialist!r} to {proj.name!r}")
            return updated
        else:
            output.die(
                f"{specialist!r} isn't assigned to {proj.name}. "
                f"Run `workforce project assign {proj.name} {specialist}` first."
            )

    return proj


def dispatch_command(
    ctx: typer.Context,
    project_ref: str = typer.Argument(
        ...,
        help="Project name, ID, or . to auto-detect from current directory",
        metavar="PROJECT",
        autocompletion=complete_project,
    ),
    ticket: str | None = typer.Argument(None, help="Ticket text in quotes."),
    specialist: str | None = typer.Option(
        None,
        "--specialist",
        help="Bypass the Manager and dispatch this specialist directly. Use for tiny tickets where you don't need planning overhead.",
        autocompletion=complete_specialist,
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
    max_retries: int = typer.Option(0, "--max-retries", help="Retry failed sub-missions N times."),
    retry_backoff: float = typer.Option(30.0, "--retry-backoff", help="Base backoff seconds between retries."),
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
    file: str | None = typer.Option(
        None, "--file", metavar="PATH",
        help="Read ticket text from PATH instead of the positional argument.",
    ),
    stdin: bool = typer.Option(
        False, "--stdin",
        help="Read ticket text from stdin.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help=(
            "Plan the work but do not dispatch. "
            "Prints the decomposition and estimated cost, then exits."
        ),
    ),
    ci: bool = typer.Option(
        False, "--ci",
        help=(
            "Non-interactive: skip prompts, suppress ANSI output, "
            "and write a JSON summary to stdout on completion."
        ),
    ),
    output_file: str | None = typer.Option(
        None, "--output-file", metavar="PATH",
        help="With --ci: also write the JSON summary to PATH.",
    ),
    require_review: bool = typer.Option(
        False, "--require-review",
        help="With --ci: fail with exit code 2 if --review was not also passed.",
    ),
    github_issue: str | None = typer.Option(
        None, "--github-issue",
        metavar="URL",
        help=(
            "Fetch the ticket text from a GitHub issue. "
            "Accepts https://github.com/owner/repo/issues/N or owner/repo#N. "
            "Mutually exclusive with positional ticket and --github-pr."
        ),
    ),
    github_pr: str | None = typer.Option(
        None, "--github-pr",
        metavar="URL",
        help=(
            "Fetch the ticket text from a GitHub PR description. "
            "Accepts https://github.com/owner/repo/pull/N or owner/repo#N. "
            "Mutually exclusive with positional ticket and --github-issue."
        ),
    ),
    open_pr: bool = typer.Option(
        False, "--open-pr",
        help=(
            "After a successful --auto-merge, create a GitHub PR for the "
            "mission branch. Requires gh CLI. Use --pr-base to set the target "
            "branch (default 'main') and --pr-draft to mark the PR as a draft."
        ),
    ),
    pr_base: str = typer.Option(
        "main", "--pr-base",
        metavar="BRANCH",
        help="Base branch for the GitHub PR opened by --open-pr. Default 'main'.",
    ),
    pr_draft: bool = typer.Option(
        False, "--pr-draft",
        help="Create the GitHub PR in draft mode (implies --open-pr).",
    ),
) -> None:
    """Dispatch a mission. The Manager plans it, then it runs.

    The Manager runs first to decide whether the ticket should fan out across
    multiple specialists in parallel, run as a sequential chain, or just go
    to one specialist. Pass --specialist to skip the Manager and dispatch
    directly to a named specialist (cheaper for tiny tickets).
    """
    # CI mode: plain-text output, implied --yes.
    if ci:
        output.set_ci_mode()
        yes = True

    # --require-review is only meaningful when --ci and --review are both set.
    if require_review and not review:
        output.die(
            "--require-review requires --review; pass --review to enable code review",
            code=2,
        )

    # --dry-run is incompatible with detached modes.
    if dry_run and (window or background):
        output.die("--dry-run is not compatible with --window or --background")

    # Resolve output_file path early (before anything that might exit).
    output_file_path: Path | None = Path(output_file) if output_file is not None else None

    # --pr-draft implies --open-pr.
    if pr_draft:
        open_pr = True

    # Resolve ticket text from whichever source was specified (including GitHub sources).
    if github_issue is not None or github_pr is not None:
        # GitHub ticket sources take precedence over --file/--stdin.
        if ticket is not None or file is not None or stdin:
            output.die(
                "conflicting ticket sources: use at most one of the positional "
                "ticket argument, --file, --stdin, --github-issue, or --github-pr"
            )
        if github_issue is not None and github_pr is not None:
            output.die("--github-issue and --github-pr are mutually exclusive")
        if github_issue is not None:
            try:
                ticket = github_mod.fetch_issue(github_issue)
            except (ValueError, RuntimeError) as e:
                output.die(str(e))
        elif github_pr is not None:
            try:
                ticket = github_mod.fetch_pr(github_pr)
            except (ValueError, RuntimeError) as e:
                output.die(str(e))

    ticket_text = _read_ticket(ticket, file, stdin)

    if window and background:
        output.die("--window and --background are mutually exclusive")
    if branch is not None and merge_into is not None:
        output.die("--branch and --merge-into are mutually exclusive (--branch sets the merge target)")
    if window or background:
        _dispatch_detached(
            project_ref=project_ref, ticket=ticket_text, specialist=specialist,
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
            ci=ci,
            output_file=output_file,
            require_review=require_review,
        )
        return

    roster_store, project_store, worktree_manager = _stores()

    # Apply global-config defaults for flags the user did not explicitly pass.
    from click import ParameterSource

    _gcfg = load_global_config()
    if ctx.get_parameter_source("max_turns") is ParameterSource.DEFAULT and _gcfg.max_turns is not None:
        max_turns = _gcfg.max_turns
    if ctx.get_parameter_source("max_cost") is ParameterSource.DEFAULT and _gcfg.max_cost is not None:
        max_cost = _gcfg.max_cost

    try:
        proj = project_store.resolve(project_ref)
    except project_mod.ProjectError as e:
        output.die(str(e))

    # Apply per-project .workforce.toml defaults for flags not explicitly passed.
    _proj_config = project_mod.load_project_config(Path(proj.repo_path))
    if _proj_config.review is not None and ctx.get_parameter_source("review") is ParameterSource.DEFAULT:
        review = _proj_config.review
    if _proj_config.auto_merge is not None and ctx.get_parameter_source("auto_merge") is ParameterSource.DEFAULT:
        auto_merge = _proj_config.auto_merge
    if _proj_config.max_turns is not None and ctx.get_parameter_source("max_turns") is ParameterSource.DEFAULT:
        max_turns = _proj_config.max_turns
    if _proj_config.max_cost is not None and ctx.get_parameter_source("max_cost") is ParameterSource.DEFAULT:
        max_cost = _proj_config.max_cost
    if _proj_config.default_specialist is not None and ctx.get_parameter_source("specialist") is ParameterSource.DEFAULT:
        specialist = _proj_config.default_specialist

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

    # Budget check: refuse or warn before any expensive planning happens.
    from workforce.budget import check_budget
    budget = check_budget(proj.id, proj)
    if not budget.allowed:
        output.die(f"budget limit reached: {budget.reason}")
    if budget.warning:
        output.warn(f"budget warning: {budget.warning}")

    # Apply per-mission cost cap from project config if it's tighter than the
    # CLI --max-cost flag.
    if proj.per_mission_limit_usd is not None:
        max_cost = min(max_cost, proj.per_mission_limit_usd)

    limits = RunLimits(
        max_turns=max_turns, max_budget_usd=max_cost, max_wall_seconds=max_wall,
        max_retries=max_retries, retry_backoff_base=retry_backoff,
    )

    # Bypass: --specialist X skips the Manager entirely.
    if specialist is not None:
        # Onboarding wizard: ensure the specialist exists and is assigned.
        # When yes=True (set by --yes or --ci), skip the wizard and die fast.
        proj = _onboard_specialist(
            specialist, proj, roster_store, project_store,
            skip_prompts=yes,
        )

        # Dry-run: print what would run and the estimated cost, then exit.
        if dry_run:
            _print_dry_run_direct(specialist, ticket_text, limits, roster_store)
            return

        _dispatch_direct(
            proj, ticket_text, roster_store.load(specialist),
            roster_store, project_store, worktree_manager, limits,
            auto_merge=auto_merge or merge_into is not None,
            merge_into=merge_into,
            review=review, max_revisions=max_revisions,
            mission_id=mission_id_override,
            base_branch=branch,
            ci=ci,
            output_file=output_file_path,
            open_pr=open_pr,
            pr_base=pr_base,
            pr_draft=pr_draft,
        )
        return

    # Manager-driven dispatch may decide to fan out across specialists in
    # parallel. For workspace projects, parallel sub-missions all share the
    # project directory (no worktree isolation) — safety comes from the
    # Manager validating non-overlapping `owns_paths` at plan time and the
    # `can_use_tool` callback enforcing those lanes at write time.
    _dispatch_with_manager(
        proj, ticket_text, roster_store, project_store, worktree_manager,
        limits=limits, skip_confirm=yes, auto_staff=auto_staff,
        auto_merge=auto_merge or merge_into is not None,
        merge_into=merge_into,
        panels=panels,
        review=review, max_revisions=max_revisions,
        base_branch=branch,
        dry_run=dry_run,
        ci=ci,
        output_file=output_file_path,
        open_pr=open_pr,
        pr_base=pr_base,
        pr_draft=pr_draft,
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
    ci: bool = False,
    output_file: Path | None = None,
    open_pr: bool = False,
    pr_base: str = "main",
    pr_draft: bool = False,
) -> None:
    """Single specialist, no Manager. The --specialist X bypass."""
    ticket_preview = _truncate(ticket, 200)
    meta_line = (
        f"[dim]specialist:[/dim] [bold]{spec.name}[/bold]  "
        f"[dim]project:[/dim] {proj.name}  "
        f"[dim]turns≤{limits.max_turns}  cost≤${limits.max_budget_usd:.2f}[/dim]"
    )
    output.raw(Panel(
        f"[italic]{ticket_preview}[/italic]\n\n{meta_line}",
        title="[bold cyan]⚡ dispatch[/bold cyan]  [dim](direct — no Manager)[/dim]",
        title_align="left",
        border_style="cyan",
        padding=(0, 1),
    ))
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
        if open_pr and meta.status is MissionStatus.COMPLETED:
            _maybe_create_pr(proj, meta, pr_base=pr_base, pr_draft=pr_draft)
    if ci:
        _write_ci_summary(_ci_summary_single(meta), output_file)
    if meta.status is not MissionStatus.COMPLETED:
        if ci and meta.status is MissionStatus.REVIEW_REJECTED:
            raise typer.Exit(code=2)
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
    ci: bool = False,
    output_file: str | None = None,
    require_review: bool = False,
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
    if ci:
        argv += ["--ci"]
    if output_file is not None:
        argv += ["--output-file", output_file]
    if require_review:
        argv += ["--require-review"]

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

    output.raw(Panel(
        f"[bold]{mission_id}[/bold]\n"
        f"[dim]Watch it:[/dim]  [bold]workforce mission tail {mission_id}[/bold]",
        title="[bold cyan]⚡ mission dispatched[/bold cyan]",
        title_align="left",
        border_style="cyan",
        padding=(0, 1),
    ))

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
    dry_run: bool = False,
    ci: bool = False,
    output_file: Path | None = None,
    open_pr: bool = False,
    pr_base: str = "main",
    pr_draft: bool = False,
) -> None:
    """Run Manager, branch on `kind`: single → mission.dispatch; else parallel."""
    if not proj.assigned_specialists and not auto_staff:
        output.die(
            f"no specialists assigned to project {proj.name!r} and --no-auto-staff. "
            f"Either assign specialists or drop --no-auto-staff so the Manager "
            "can hire from templates as needed."
        )

    ticket_preview = _truncate(ticket, 200)
    meta_line = (
        f"[dim]Manager → auto-assign  "
        f"project:[/dim] {proj.name}  "
        f"[dim]turns≤{limits.max_turns}  cost≤${limits.max_budget_usd:.2f}[/dim]"
    )
    output.raw(Panel(
        f"[italic]{ticket_preview}[/italic]\n\n{meta_line}",
        title="[bold cyan]⚡ dispatch[/bold cyan]  [dim](Manager planning...)[/dim]",
        title_align="left",
        border_style="cyan",
        padding=(0, 1),
    ))

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
        # Exit code 4 = manager error in CI mode.
        output.die(f"manager: {e}", code=4 if ci else 1)

    output.info(
        f"[dim]manager: kind={decomp.kind.value}  cost=${manager_cost:.4f}  "
        f"({decomp.rationale})[/dim]"
    )

    # Dry-run: resolve specialists for display, print table + cost, then exit.
    if dry_run:
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
        rows = [(r.task.id, r.specialist.name, r.staffing_action) for r in resolved]
        _print_dry_run_manager(decomp, rows, roster_store)
        return

    # Branch on kind.
    if decomp.kind is DecompositionKind.SINGLE:
        _dispatch_after_manager_single(
            proj, ticket, decomp, manager_cost,
            roster_store, project_store, worktree_manager,
            limits=limits, auto_staff=auto_staff,
            auto_merge=auto_merge, merge_into=merge_into,
            review=review, max_revisions=max_revisions,
            base_branch=base_branch,
            ci=ci,
            output_file=output_file,
            open_pr=open_pr, pr_base=pr_base, pr_draft=pr_draft,
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
            ci=ci,
            output_file=output_file,
            open_pr=open_pr, pr_base=pr_base, pr_draft=pr_draft,
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
    ci: bool = False,
    output_file: Path | None = None,
    open_pr: bool = False,
    pr_base: str = "main",
    pr_draft: bool = False,
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
        if open_pr and meta.status is MissionStatus.COMPLETED:
            _maybe_create_pr(proj, meta, pr_base=pr_base, pr_draft=pr_draft)
    if ci:
        _write_ci_summary(_ci_summary_single(meta), output_file)
    if meta.status is not MissionStatus.COMPLETED:
        if ci and meta.status is MissionStatus.REVIEW_REJECTED:
            raise typer.Exit(code=2)
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
    ci: bool = False,
    output_file: Path | None = None,
    open_pr: bool = False,
    pr_base: str = "main",
    pr_draft: bool = False,
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
                ci=ci,
                output_file=output_file,
                open_pr=open_pr, pr_base=pr_base, pr_draft=pr_draft,
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
        if open_pr and result.parent_meta.status is ParallelStatus.COMPLETED:
            # For parallel missions, create one PR per successfully completed
            # sub-mission branch.
            for sub in result.sub_metas:
                if sub.status is MissionStatus.COMPLETED and sub.branch:
                    _maybe_create_pr(proj, sub, pr_base=pr_base, pr_draft=pr_draft)

    if ci:
        _write_ci_summary(_ci_summary_parallel(result.parent_meta, result.sub_metas), output_file)

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


def _maybe_create_pr(
    proj: project_mod.Project,
    meta: MissionMeta,
    *,
    pr_base: str,
    pr_draft: bool,
) -> None:
    """Create a GitHub PR for *meta*'s branch.

    Reads title from the first line of result.md (or the first commit subject),
    body from the full result.md content (truncated to 65 000 chars), and calls
    ``github.create_pr``.  Prints the resulting PR URL on success or a warning
    on failure.
    """
    if meta.branch is None:
        output.warn("--open-pr skipped: no branch (workspace mission)")
        return

    mp = mission.mission_paths(proj.id, meta.mission_id)
    body = ""
    title = ""

    if mp.result.exists():
        body = mp.result.read_text(encoding="utf-8")
        first_line = body.splitlines()[0].lstrip("#").strip() if body.splitlines() else ""
        title = first_line

    if not title and meta.commits:
        title = meta.commits[0].subject.strip()

    if not title:
        title = f"workforce mission {meta.mission_id}"

    try:
        pr_url = github_mod.create_pr(
            repo_path=str(proj.repo_path),
            branch=meta.branch,
            title=title,
            body=body,
            base=pr_base,
            draft=pr_draft,
        )
        output.success(f"PR opened: {pr_url}")
    except RuntimeError as e:
        output.warn(f"--open-pr failed: {e}")


def _print_parallel_summary(parent: ParallelMissionMeta, subs: list[MissionMeta]) -> None:
    """Print the per-task status table and total cost for a parallel mission."""
    status_border = {
        ParallelStatus.COMPLETED: "green",
        ParallelStatus.FAILED: "red",
        ParallelStatus.PARTIAL: "yellow",
    }.get(parent.status, "dim")

    # Summary grid
    summary_grid = Table.grid(padding=(0, 2))
    summary_grid.add_column(style="bold dim")
    summary_grid.add_column()
    summary_grid.add_row(
        "status",
        _PARALLEL_STATUS_BADGES.get(parent.status, _PARALLEL_STATUS_STYLES[parent.status]),
    )
    summary_grid.add_row("manager cost", f"${parent.manager_cost_usd:.4f}")

    if subs:
        table = Table(show_header=True, header_style="bold dim", box=box.SIMPLE)
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
            sub_cost = m.cost_usd + m.review_cost_usd
            total_cost += sub_cost
            table.add_row(
                ref.task_id,
                m.specialist,
                _STATUS_STYLES[m.status],
                f"${sub_cost:.4f}",
                str(m.turn_count),
                str(len(m.commits)),
                m.branch,
            )
        summary_grid.add_row("total cost", f"${total_cost:.4f}")

        output.raw(Panel(
            summary_grid,
            title=f"[bold]{parent.parent_mission_id}[/bold]",
            title_align="left",
            border_style=status_border,
            padding=(0, 1),
        ))
        output.raw(Panel(
            table,
            title="[bold]sub-missions[/bold]",
            title_align="left",
            border_style="dim",
            padding=(0, 0),
        ))
    else:
        output.raw(Panel(
            summary_grid,
            title=f"[bold]{parent.parent_mission_id}[/bold]",
            title_align="left",
            border_style=status_border,
            padding=(0, 1),
        ))


def _print_summary(meta: mission.MissionMeta) -> None:
    """Print a single-mission status block (branch, cost, turns, review verdict)."""
    status_border = {
        MissionStatus.COMPLETED: "green",
        MissionStatus.ERROR: "red",
        MissionStatus.REVIEW_REJECTED: "red",
        MissionStatus.WALL_TIMEOUT: "yellow",
        MissionStatus.INTERRUPTED: "yellow",
    }.get(meta.status, "dim")

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold dim")
    grid.add_column()
    grid.add_row("status", _STATUS_BADGES.get(meta.status, _STATUS_STYLES[meta.status]))
    if meta.branch is None:
        grid.add_row("workspace", meta.worktree_path or "[dim](unknown)[/dim]")
    else:
        grid.add_row("branch", meta.branch)
    dur = meta.duration_seconds
    if dur >= 60:
        grid.add_row("duration", f"{int(dur) // 60}m {int(dur) % 60}s")
    else:
        grid.add_row("duration", f"{dur:.0f}s")
    grid.add_row("cost", f"${meta.cost_usd:.4f}")
    grid.add_row("turns", str(meta.turn_count))
    if meta.branch is not None:
        grid.add_row("commits", str(len(meta.commits)))

    if meta.reviews:
        approved = meta.reviews[-1].approved
        verdict = "[green]approved[/green]" if approved else "[red]rejected[/red]"
        grid.add_row(
            "review",
            f"{verdict}  [dim]{len(meta.reviews)} round(s)  "
            f"review_cost=${meta.review_cost_usd:.4f}[/dim]",
        )
        if not approved and meta.reviews[-1].issues:
            issues_text = "\n".join(f"• {i}" for i in meta.reviews[-1].issues[:5])
            grid.add_row("issues", f"[red]{issues_text}[/red]")

    if meta.error_detail:
        grid.add_row("error", f"[red]{meta.error_detail}[/red]")

    if meta.memory_delta_captured:
        grid.add_row("memory", "delta captured")

    artifacts_path = paths.project_dir(meta.project_id) / "missions" / meta.mission_id
    grid.add_row("artifacts", str(artifacts_path))

    output.raw(Panel(
        grid,
        title=f"[bold]{meta.mission_id}[/bold]",
        title_align="left",
        border_style=status_border,
        padding=(0, 1),
    ))

    if meta.branch is not None and meta.commits and len(meta.commits) < 2:
        output.warn(
            "only one commit — check if the specialist is committing as it goes"
        )
