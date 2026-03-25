"""CLI commands for projects: add, assign, unassign, list, show, forget."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from workforce import output, paths, project
from workforce.specialist import RosterStore


sub = typer.Typer(
    name="project",
    help="Register repos and assign specialists to them.",
    no_args_is_help=True,
)


def _project_store() -> project.ProjectStore:
    paths.ensure_layout()
    return project.ProjectStore()


def _roster_store() -> RosterStore:
    paths.ensure_layout()
    return RosterStore()


@sub.command("add")
def add(
    path: Path = typer.Argument(
        ..., help="Path to a git repository.", exists=True, file_okay=False
    ),
    name: str | None = typer.Option(
        None, "--name", help="Display name (default: repo basename)."
    ),
) -> None:
    """Register a git repository as a Workforce project."""
    repo_path = path.resolve()

    if not project.is_git_repo(repo_path):
        output.die(f"{repo_path} is not a git repository (no .git found)")

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
        )
        store.save(proj)
    except (project.ProjectError, ValueError) as e:
        output.die(str(e))

    # Write the marker file so future operations resolve to the same id even
    # if the repo is moved. Best-effort — if the repo is read-only we warn.
    try:
        project.write_marker(repo_path, project_id)
    except OSError as e:
        output.warn(
            f"could not write {project.MARKER_FILENAME} marker in repo: {e}. "
            "If you move the repo, the project id will change."
        )

    output.success(
        f"registered project {proj.name!r} (id {proj.id}) at {repo_path}"
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
    meta.add_row("repo path", proj.repo_path)
    meta.add_row(
        "assigned specialists",
        ", ".join(proj.assigned_specialists) if proj.assigned_specialists else "(none)",
    )
    meta.add_row("default model", proj.default_model or "(roster default)")

    repo_exists = Path(proj.repo_path).is_dir()
    meta.add_row("repo present", "yes" if repo_exists else "[red]MISSING[/red]")

    missions = pstore.missions_dir(proj.id)
    n_missions = len([p for p in missions.iterdir() if p.is_dir()]) if missions.is_dir() else 0
    meta.add_row("recorded missions", str(n_missions))

    output.raw(Panel(meta, title=f"project: {proj.name}", title_align="left"))


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
