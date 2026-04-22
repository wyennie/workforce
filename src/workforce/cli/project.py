"""CLI commands for projects: add, assign, unassign, list, show, forget, tail."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Literal

import typer
from rich.panel import Panel
from rich.table import Table

from workforce import output, paths, project
from workforce.project import load_project_config
from workforce.specialist import RosterStore
from workforce.worktree import (
    WorktreeManager,
    find_workforce_branches,
    has_commits,
)

from .mission import render_labeled_event

sub = typer.Typer(
    name="project",
    help="Register repos and assign specialists to them.",
    no_args_is_help=True,
)


def _project_store() -> project.ProjectStore:
    """Ensure the Workforce data layout exists and return a ProjectStore."""
    paths.ensure_layout()
    return project.ProjectStore()


def _roster_store() -> RosterStore:
    """Ensure the Workforce data layout exists and return a RosterStore."""
    paths.ensure_layout()
    return RosterStore()


@sub.command("add")
def add(
    path: Path = typer.Argument(
        ..., help="Path to a git repository or a plain working directory.",
        exists=True, file_okay=False,
    ),
    name: str | None = typer.Option(
        None, "--name", help="Display name (default: directory basename)."
    ),
    workspace: bool = typer.Option(
        False, "--workspace",
        help=(
            "Force workspace kind even if `<path>/.git` exists. Missions run in "
            "the directory directly with no worktree, no commits, and no "
            "auto-merge."
        ),
    ),
    repo: bool = typer.Option(
        False, "--repo",
        help=(
            "Force repo kind. Fails if `<path>/.git` is missing. The default "
            "auto-detects from `.git` presence — pass this when you want to "
            "make the choice explicit in scripts."
        ),
    ),
) -> None:
    """Register a directory as a Workforce project.

    By default the kind is inferred from the directory: `.git` present → repo
    (worktrees, commits, the works); otherwise → workspace (plain working
    directory, file outputs only). Pass `--workspace` or `--repo` to override.
    """
    if workspace and repo:
        output.die("--workspace and --repo are mutually exclusive")

    repo_path = path.resolve()
    has_git = project.is_git_repo(repo_path)

    if repo and not has_git:
        output.die(
            f"{repo_path} is not a git repository (no .git found). "
            "Initialize it first or drop the --repo flag to register it as a "
            "workspace."
        )

    if workspace:
        kind: Literal["repo", "workspace"] = "workspace"
    elif repo:
        kind = "repo"
    else:
        kind = "repo" if has_git else "workspace"

    try:
        project_id = project.resolve_project_id(repo_path)
    except project.ProjectError as e:
        output.die(str(e))

    store = _project_store()

    display_name = name or repo_path.name
    try:
        proj = project.Project(
            id=project_id,
            name=display_name,
            repo_path=str(repo_path),
            kind=kind,
        )
        store.save(proj)
    except (project.ProjectError, ValueError) as e:
        output.die(str(e))

    # Write the marker file so future operations resolve to the same id even
    # if the directory is moved. Best-effort — if it's read-only we warn.
    try:
        project.write_marker(repo_path, project_id)
    except OSError as e:
        output.warn(
            f"could not write {project.MARKER_FILENAME} marker: {e}. "
            "If you move the directory, the project id will change."
        )

    label = "workspace" if kind == "workspace" else "project"
    output.success(
        f"registered {label} {proj.name!r} (id {proj.id}) at {repo_path}"
    )

    if kind == "repo" and not has_commits(repo_path):
        output.warn(
            "this repo has no commits yet. Workforce can't dispatch missions "
            "until there's at least one commit. Run "
            "`git -C "
            + str(repo_path)
            + " commit --allow-empty -m initial` to bootstrap."
        )


@sub.command("assign")
def assign(
    project_ref: str = typer.Argument(..., help="Project name or id.", metavar="PROJECT"),
    specialists: list[str] = typer.Argument(..., help="Specialist names to assign."),
) -> None:
    """Assign one or more specialists to a project."""
    pstore = _project_store()
    rstore = _roster_store()

    try:
        proj = pstore.resolve(project_ref)
    except project.ProjectError as e:
        output.die(str(e))

    missing = [s for s in specialists if not rstore.exists(s)]
    if missing:
        output.die(
            "unknown specialist(s): " + ", ".join(missing)
            + " — `workforce roster` lists what's available"
        )

    added: list[str] = []
    already: list[str] = []
    for s in specialists:
        if s in proj.assigned_specialists:
            already.append(s)
        else:
            proj.assigned_specialists.append(s)
            added.append(s)

    pstore.save(proj, overwrite=True)

    if added:
        output.success(f"assigned to {proj.name}: {', '.join(added)}")
    if already:
        output.info(f"already assigned: {', '.join(already)}")


@sub.command("unassign")
def unassign(
    project_ref: str = typer.Argument(..., help="Project name or id.", metavar="PROJECT"),
    specialist: str = typer.Argument(..., help="Specialist name."),
) -> None:
    """Remove a specialist from a project."""
    pstore = _project_store()
    try:
        proj = pstore.resolve(project_ref)
    except project.ProjectError as e:
        output.die(str(e))

    if specialist not in proj.assigned_specialists:
        output.die(f"{specialist!r} is not assigned to {proj.name}")

    proj.assigned_specialists.remove(specialist)
    pstore.save(proj, overwrite=True)
    output.success(f"unassigned {specialist} from {proj.name}")


@sub.command("list")
def list_() -> None:
    """List all registered projects."""
    pstore = _project_store()
    projects = pstore.list()
    if not projects:
        output.info("no projects registered — try `workforce project add <path>`")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("id")
    table.add_column("specialists")
    table.add_column("path", overflow="fold")

    for p in projects:
        assigned = ", ".join(p.assigned_specialists) if p.assigned_specialists else "[dim](none)[/dim]"
        table.add_row(p.name, p.id, assigned, p.repo_path)

    output.print_table(table)


@sub.command("show")
def show(project_ref: str = typer.Argument(..., help="Project name or id.", metavar="PROJECT")) -> None:
    """Show project details and assigned specialists."""
    pstore = _project_store()
    try:
        proj = pstore.resolve(project_ref)
    except project.ProjectError as e:
        output.die(str(e))

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="bold")
    meta.add_column()
    meta.add_row("name", proj.name)
    meta.add_row("id", proj.id)
    meta.add_row("kind", proj.kind)
    path_label = "workspace path" if proj.kind == "workspace" else "repo path"
    meta.add_row(path_label, proj.repo_path)
    meta.add_row(
        "assigned specialists",
        ", ".join(proj.assigned_specialists) if proj.assigned_specialists else "(none)",
    )
    meta.add_row("default model", proj.default_model or "(roster default)")

    dir_exists = Path(proj.repo_path).is_dir()
    presence_label = "workspace present" if proj.kind == "workspace" else "repo present"
    meta.add_row(presence_label, "yes" if dir_exists else "[red]MISSING[/red]")

    # Walk missions for counts + total cost across both single and parent metas.
    import json as _json
    missions = pstore.missions_dir(proj.id)
    n_missions = 0
    total_cost = 0.0
    if missions.is_dir():
        for d in missions.iterdir():
            if not d.is_dir():
                continue
            n_missions += 1
            mp = d / "meta.json"
            if not mp.is_file():
                continue
            try:
                data = _json.loads(mp.read_text())
            except (OSError, ValueError):
                continue
            # Only count cost_usd (MissionMeta / sub-missions). Skip
            # manager_cost_usd on ParallelMissionMeta parent dirs: that cost
            # is already embedded in each sub-mission's cost_usd, so counting
            # it here too would double the decomposition spend.
            if "cost_usd" in data:
                total_cost += float(data["cost_usd"])
    meta.add_row("recorded missions", str(n_missions))
    meta.add_row("total cost (usd)", f"{total_cost:.4f}")

    output.raw(Panel(meta, title=f"project: {proj.name}", title_align="left"))


@sub.command("nuke")
def nuke(
    project_ref: str = typer.Argument(..., help="Project name or id.", metavar="PROJECT"),
    also_memory: bool = typer.Option(
        False, "--also-memory",
        help="Also delete per-specialist project memory files. Default: keep memory.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be deleted, change nothing.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Wipe all branches, worktrees, and mission artifacts for a project.

    Useful for "test, fail, retry from clean state" loops. Keeps the project
    registration, the global roster, and (by default) per-specialist project
    memory. Pass --also-memory to wipe memory too.
    """
    pstore = _project_store()
    try:
        proj = pstore.resolve(project_ref)
    except project.ProjectError as e:
        output.die(str(e))

    repo = Path(proj.repo_path)
    if not repo.is_dir():
        label = "workspace" if proj.kind == "workspace" else "repo"
        output.die(f"project {proj.name!r} {label} path missing: {repo}")

    is_workspace = proj.kind == "workspace"

    # What we'd delete
    worktrees_dir = WorktreeManager().project_worktrees_dir(proj.id)
    missions_dir = pstore.missions_dir(proj.id)
    memory_dir = pstore.memory_dir(proj.id)

    branches: list[str] = []
    if not is_workspace:
        try:
            branches = find_workforce_branches(repo)
        except subprocess.CalledProcessError as e:
            output.warn(f"could not list branches: {e}")

    worktrees = (
        sorted(p.name for p in worktrees_dir.iterdir() if p.is_dir())
        if (not is_workspace and worktrees_dir.is_dir()) else []
    )
    missions = (
        sorted(p.name for p in missions_dir.iterdir() if p.is_dir())
        if missions_dir.is_dir() else []
    )
    memory_files = (
        sorted(p.name for p in memory_dir.iterdir() if p.is_file())
        if memory_dir.is_dir() else []
    )

    output.info(f"[bold]project nuke[/bold]: {proj.name} (id {proj.id})")
    path_label = "workspace" if is_workspace else "repo"
    output.info(f"  {path_label}:      {repo}")
    if not is_workspace:
        output.info(f"  branches:  {len(branches)} workforce/*")
        output.info(f"  worktrees: {len(worktrees)}")
    output.info(f"  missions:  {len(missions)}")
    if also_memory:
        output.info(f"  memory:    {len(memory_files)} file(s) [bold red](will be wiped)[/bold red]")
    else:
        output.info(f"  memory:    {len(memory_files)} file(s) [dim](kept)[/dim]")

    if not (branches or worktrees or missions or (also_memory and memory_files)):
        output.info("nothing to nuke")
        return

    if dry_run:
        output.info("[dim](dry-run — nothing changed)[/dim]")
        return

    if not yes:
        confirm = typer.confirm(
            "Wipe these and start fresh? (project registration + roster are kept)",
            default=False,
        )
        if not confirm:
            output.info("aborted")
            raise typer.Exit()

    if not is_workspace:
        # 1. Drop worktree directories. Do this BEFORE branch deletion so git
        #    isn't holding the branches via the worktrees.
        if worktrees_dir.is_dir():
            shutil.rmtree(worktrees_dir, ignore_errors=True)
            output.success(f"removed {len(worktrees)} worktree dir(s)")

        # 2. Tell git to forget the worktree registrations.
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=repo, capture_output=True, text=True, check=False,
            )
        except OSError as e:
            output.warn(f"git worktree prune failed: {e}")

        # 3. Force-delete the branches.
        deleted = 0
        for branch in branches:
            r = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=repo, capture_output=True, text=True, check=False,
            )
            if r.returncode == 0:
                deleted += 1
            else:
                err = (r.stderr.strip() or r.stdout.strip())[:200]
                output.warn(f"could not delete {branch}: {err}")
        if deleted:
            output.success(f"deleted {deleted} branch(es)")

    # 4. Wipe missions.
    if missions_dir.is_dir():
        shutil.rmtree(missions_dir)
        missions_dir.mkdir(parents=True, exist_ok=True)
        output.success(f"wiped {len(missions)} mission artifact dir(s)")

    # 5. Wipe memory if asked.
    if also_memory and memory_dir.is_dir():
        shutil.rmtree(memory_dir)
        memory_dir.mkdir(parents=True, exist_ok=True)
        output.success(f"wiped {len(memory_files)} project-memory file(s)")

    output.success(f"project {proj.name!r} reset to a clean slate")


@sub.command("forget")
def forget(
    project_ref: str = typer.Argument(..., help="Project name or id.", metavar="PROJECT"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Unregister a project (deletes its memory and mission history).

    Does not touch the repo itself or its `.workforce-project-id` marker.
    """
    pstore = _project_store()
    try:
        proj = pstore.resolve(project_ref)
    except project.ProjectError as e:
        output.die(str(e))

    if not yes:
        confirm = typer.confirm(
            f"Forget project {proj.name!r} (id {proj.id})? "
            "This deletes all memory and mission history for this project.",
            default=False,
        )
        if not confirm:
            output.info("aborted")
            raise typer.Exit()

    pstore.delete(proj.id)
    output.success(f"forgot project {proj.name}")


# ----- config (per-project .workforce.toml) ---------------------------------


@sub.command("config")
def config(
    project_ref: str = typer.Argument(
        ...,
        help="Project name, ID, or . to auto-detect from current directory",
        metavar="PROJECT",
    ),
) -> None:
    """Show the active per-project configuration for a project.

    Reads ``.workforce.toml`` from the project's repo root and pretty-prints
    the resolved values.  Fields not present in the file are shown as their
    defaults.
    """
    pstore = _project_store()
    try:
        proj = pstore.resolve(project_ref)
    except project.ProjectError as e:
        output.die(str(e))

    cfg = load_project_config(Path(proj.repo_path))

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()

    def _fmt(val: object) -> str:
        if val is None:
            return "[dim](not set)[/dim]"
        return str(val)

    table.add_row("default_specialist", _fmt(cfg.default_specialist))
    table.add_row("review", _fmt(cfg.review))
    table.add_row("auto_merge", _fmt(cfg.auto_merge))
    table.add_row("max_turns", _fmt(cfg.max_turns))
    table.add_row("max_cost", _fmt(cfg.max_cost))

    output.raw(Panel(table, title=f"project config: {proj.name}", title_align="left"))


# ----- tail (multi-mission live stream) -------------------------------------


@sub.command("tail")
def project_tail(
    project_ref: str = typer.Argument(..., help="Project name or id.", metavar="PROJECT"),
    show_thinking: bool = typer.Option(
        False, "--show-thinking", help="Include thinking blocks in the stream."
    ),
    poll_seconds: float = typer.Option(
        0.5, "--poll", help="How often to check for new events / new missions.",
    ),
) -> None:
    """Stream events from ALL missions in a project, interleaved with labels.

    Run this once per project (the Manager session opens it for you in a
    separate window). New missions are picked up automatically as their
    directories appear; events are tagged `[short-id/specialist]` so you can
    tell who's saying what when several workers run in parallel.
    """
    paths.ensure_layout()
    pstore = _project_store()
    try:
        proj = pstore.resolve(project_ref)
    except project.ProjectError as e:
        output.die(str(e))

    missions_dir = pstore.missions_dir(proj.id)
    output.info(
        f"[bold]tailing project {proj.name}[/bold] — all missions "
        f"[dim]({missions_dir})[/dim]"
    )
    output.rule()

    # Per-mission read positions and labels.
    positions: dict[str, int] = {}
    labels: dict[str, str] = {}

    def label_for(mid: str) -> str:
        short = mid[-8:] if len(mid) > 8 else mid
        meta_path = missions_dir / mid / "meta.json"
        if meta_path.is_file():
            try:
                data = json.loads(meta_path.read_text())
                spec = data.get("specialist") or "?"
                return f"{short}/{spec}"
            except (OSError, ValueError):
                pass
        return f"{short}/…"

    try:
        while True:
            # Discover new missions; refresh labels for ones whose meta.json
            # appeared after we attached.
            if missions_dir.is_dir():
                for d in sorted(missions_dir.iterdir()):
                    if not d.is_dir():
                        continue
                    mid = d.name
                    if mid not in positions:
                        positions[mid] = 0
                        labels[mid] = label_for(mid)
                        output.info(
                            f"[bold cyan]+ attached: {labels[mid]}[/bold cyan]"
                        )
                    elif labels[mid].endswith("/…"):
                        new_label = label_for(mid)
                        if not new_label.endswith("/…"):
                            labels[mid] = new_label

            # Read new events from each attached mission.
            for mid in list(positions.keys()):
                events_path = missions_dir / mid / "events.jsonl"
                if not events_path.is_file():
                    continue
                try:
                    with events_path.open() as f:
                        f.seek(positions[mid])
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                evt = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            render_labeled_event(
                                labels[mid], evt, show_thinking=show_thinking
                            )
                        positions[mid] = f.tell()
                except FileNotFoundError:
                    pass

            time.sleep(poll_seconds)
    except KeyboardInterrupt:
        output.info("[dim](stopped)[/dim]")
