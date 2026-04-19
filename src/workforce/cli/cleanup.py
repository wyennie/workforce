"""Cleanup commands: mission clean, mission prune, branches prune.

Each is destructive, so all of them gate on confirmation flags or `--dry-run`.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
from pathlib import Path

import typer

from workforce import output, paths
from workforce import project as project_mod
from workforce.mission import MissionMeta, MissionStatus
from workforce.parallel import ParallelMissionMeta
from workforce.worktree import WorktreeManager, current_branch, find_workforce_branches

from ._common import _find_mission, _list_project_missions


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
    """Parse a compact duration string into a ``timedelta``.

    Accepted units: ``h`` (hours), ``d`` (days), ``w`` (weeks),
    ``m`` (months, treated as 30 days).  Examples: ``7d``, ``24h``, ``2w``, ``1m``.

    Args:
        s: Duration string to parse.

    Returns:
        The corresponding timedelta.

    Raises:
        typer.BadParameter: If *s* does not match the expected pattern.
    """
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
    """Parse a ``'YYYY-MM-DDTHH:MM:SSZ'`` timestamp into a UTC-aware datetime.

    MissionMeta writes timestamps in this format.  Python 3.11+ accepts ``Z``
    natively; the replace keeps compatibility should that ever change.

    Args:
        s: ISO 8601 timestamp string ending in ``Z``.

    Returns:
        UTC-aware datetime object.
    """
    # MissionMeta writes 'YYYY-MM-DDTHH:MM:SSZ' — fromisoformat in 3.11+
    # accepts 'Z' since 3.11. We require 3.11 anyway.
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


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
        target = current_branch(repo)
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
