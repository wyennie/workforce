"""Workforce CLI entry point.

Commands are defined in `cli_*.py` modules and registered here.
"""

from __future__ import annotations

import typer
from rich.table import Table

from workforce import cli_mission, cli_project, cli_roster, doctor, output
from workforce.version import __version__

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

app.command("hire")(cli_roster.hire)
app.command("fire")(cli_roster.fire)
app.command("roster")(cli_roster.roster)
app.command("show")(cli_roster.show)
app.command("templates")(cli_roster.templates)
app.command("refresh")(cli_roster.refresh)


# ----- project --------------------------------------------------------------

app.add_typer(cli_project.sub)


# ----- mission --------------------------------------------------------------

app.command("dispatch")(cli_mission.dispatch_command)
app.command("missions")(cli_mission.missions_command)
app.command("replay")(cli_mission.replay_command)
app.add_typer(cli_mission.mission_sub)
app.add_typer(cli_mission.branches_sub)
