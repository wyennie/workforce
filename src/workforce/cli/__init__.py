"""Workforce CLI entry point.

Each command group lives in its own module under this package; this file just
builds the Typer app and registers them.
"""

from __future__ import annotations

import typer
from rich.table import Table

from workforce import doctor, output, paths
from workforce import project as project_mod
from workforce.specialist import RosterStore
from workforce.version import __version__

from . import cleanup, config, dispatch, manage, mission, project, roster

app = typer.Typer(
    name="workforce",
    help="A persistent roster of Claude specialists, dispatchable on tickets.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        output.info(f"workforce {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Workforce: a staffing agency for AI engineers."""


# ----- doctor ---------------------------------------------------------------


@app.command("doctor")
def doctor_command() -> None:
    """Verify the environment is ready for Workforce."""
    checks = doctor.run_all()

    table = Table(show_header=True, header_style="bold")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail", overflow="fold")

    style = {
        doctor.Status.OK: "[green]ok[/green]",
        doctor.Status.WARN: "[yellow]warn[/yellow]",
        doctor.Status.FAIL: "[red]fail[/red]",
    }
    for c in checks:
        table.add_row(c.name, style[c.status], c.detail)

    output.print_table(table)

    summary = doctor.worst(checks)
    if summary is doctor.Status.FAIL:
        output.fail("doctor: one or more checks failed")
        raise typer.Exit(code=1)
    if summary is doctor.Status.WARN:
        output.warn("doctor: passed with warnings")
        return
    output.success("doctor: all checks passed")


# ----- roster ---------------------------------------------------------------

app.command("hire")(roster.hire)
app.command("fire")(roster.fire)
app.command("roster")(roster.roster)
app.command("show")(roster.show)
app.command("templates")(roster.templates)
app.command("refresh")(roster.refresh)


# ----- config ---------------------------------------------------------------

app.add_typer(config.sub)


# ----- project --------------------------------------------------------------

app.add_typer(project.sub)


# ----- mission --------------------------------------------------------------

app.command("dispatch")(dispatch.dispatch_command)
app.command("missions")(mission.missions_command)
app.command("replay")(mission.replay_command)

mission_sub = typer.Typer(
    name="mission",
    help="Inspect and clean up individual missions.",
    no_args_is_help=True,
)
mission_sub.command("show")(mission.mission_show)
mission_sub.command("tail")(mission.mission_tail)
mission_sub.command("clean")(cleanup.mission_clean)
mission_sub.command("prune")(cleanup.mission_prune)
app.add_typer(mission_sub)

branches_sub = typer.Typer(
    name="branches",
    help="Inspect and clean up workforce/* branches in a project.",
    no_args_is_help=True,
)
branches_sub.command("prune")(cleanup.branches_prune)
app.add_typer(branches_sub)


# ----- manage (interactive Manager chat) ------------------------------------


@app.command("manage")
def manage_command(
    project_ref: str = typer.Argument(..., help="Project name or id.", metavar="PROJECT"),
    yolo: bool = typer.Option(
        False, "--yolo",
        help=(
            "Skip per-tool permission prompts (bypassPermissions). The Manager "
            "can dispatch and edit without asking. Use with care; default is "
            "to confirm before each tool call."
        ),
    ),
    branch: str | None = typer.Option(
        None,
        "--branch",
        metavar="BRANCH",
        help=(
            "Per-session staging branch. Every mission the Manager dispatches "
            "forks from BRANCH and merges back into BRANCH; main is never "
            "touched. BRANCH is created from current HEAD if it doesn't exist."
        ),
    ),
) -> None:
    """Open an interactive Manager chat session for a project.

    Talk with the Manager like you would with Claude Code. It dispatches
    workers via `workforce dispatch ... --window`, so each spawned mission
    pops up its own terminal window streaming the worker's output. The
    Manager carries context across turns and can answer questions about
    ongoing or past missions.
    """
    paths.ensure_layout()
    pstore = project_mod.ProjectStore()
    rstore = RosterStore()
    try:
        proj = pstore.resolve(project_ref)
    except project_mod.ProjectError as e:
        output.die(str(e))
    code = manage.manage_command_main(proj, rstore, yolo=yolo, branch=branch)
    raise typer.Exit(code=code)
