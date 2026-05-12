"""CLI wrappers around `parallel.auto_merge*` — printing, conflict help, drift audit.

These are pure UX helpers; the actual merge logic lives in `parallel`.
"""

from __future__ import annotations

from pathlib import Path

from workforce import output, parallel
from workforce import project as project_mod
from workforce.mission import MissionMeta, MissionStatus
from workforce.parallel import ParallelMissionMeta, ParallelStatus, merge_plan


def _run_auto_merge_single(
    proj: project_mod.Project,
    meta: MissionMeta,
    *,
    target: str | None = None,
) -> None:
    """Merge a single completed mission's branch into the current or target branch.

    Args:
        proj: The project the mission belongs to.
        meta: Metadata for the completed mission; skipped if not COMPLETED.
        target: Branch to merge into. ``None`` uses the repo's current branch.
    """
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
    """Merge all completed sub-mission branches from a parallel run.

    Args:
        proj: The project the missions belong to.
        parent: Parent parallel mission metadata; skipped if status is not COMPLETED.
        subs: Individual sub-mission metadata objects in the merge order.
        target: Branch to merge into. ``None`` uses the repo's current branch.
    """
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
    """Execute a merge plan and print per-step results.

    On failure, prints guided conflict-resolution instructions for the first
    step that has conflicting files.

    Args:
        proj: The project whose repo to merge within.
        plan: Ordered list of branches (MergeStep objects) to merge.
        target: Branch to merge into; ``None`` means merge into the current branch.
    """
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
        cur = getattr(parallel, "_current_branch")(repo) or "HEAD"
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
    """Print an ordered list of ``git merge`` commands for a parallel mission set.

    Completed sub-missions are shown as runnable commands. Sub-missions that did
    not complete cleanly are listed as warnings to skip.

    Args:
        parent: Parent parallel mission metadata.
        subs: Individual sub-mission metadata objects.
    """
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
