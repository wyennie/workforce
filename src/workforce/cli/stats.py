"""``workforce stats`` command — aggregate mission statistics."""

from __future__ import annotations

import json

import typer
from rich.table import Table

from workforce import output, paths
from workforce.stats import query_stats


def stats_command(
    project: str | None = typer.Option(
        None, "--project", metavar="PROJECT", help="Filter to a specific project id or name."
    ),
    specialist: str | None = typer.Option(
        None, "--specialist", metavar="NAME", help="Filter to a specific specialist."
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        metavar="DATE",
        help="Only include missions started on or after this ISO date (e.g. 2026-05-01).",
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Output machine-readable JSON instead of a table."
    ),
) -> None:
    """Show aggregated mission statistics across all projects.

    Displays one row per specialist with total missions, cost, average duration,
    and success rate. Pass --json for machine-readable output.
    """
    paths.ensure_layout()

    # Resolve project name → id if needed
    project_id: str | None = None
    if project is not None:
        from workforce import project as project_mod

        pstore = project_mod.ProjectStore()
        try:
            proj = pstore.resolve(project)
            project_id = proj.id
        except project_mod.ProjectError as e:
            output.die(str(e))

    result = query_stats(
        project_id=project_id,
        specialist_name=specialist,
        since_date=since,
    )

    if as_json:
        rows = []
        for sp in sorted(result.by_specialist.values(), key=lambda s: s.specialist):
            rows.append(
                {
                    "specialist": sp.specialist,
                    "missions": sp.mission_count,
                    "completed": sp.completed,
                    "failed": sp.failed,
                    "interrupted": sp.interrupted,
                    "reviewer_rejections": sp.reviewer_rejections,
                    "total_cost_usd": sp.total_cost_usd,
                    "avg_duration_seconds": sp.avg_duration_seconds,
                    "avg_turns": sp.avg_turns,
                    "success_rate": sp.success_rate,
                }
            )
        payload = {
            "total_missions": result.total_missions,
            "total_cost_usd": result.total_cost_usd,
            "by_project": result.by_project,
            "by_specialist": rows,
        }
        output.info(json.dumps(payload, indent=2))
        return

    if not result.by_specialist:
        output.info("no missions recorded" + (" (filters may be too narrow)" if any([project, specialist, since]) else ""))
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("specialist")
    table.add_column("missions", justify="right")
    table.add_column("cost (USD)", justify="right")
    table.add_column("avg duration", justify="right")
    table.add_column("avg turns", justify="right")
    table.add_column("success rate", justify="right")

    for sp in sorted(result.by_specialist.values(), key=lambda s: s.specialist):
        dur = sp.avg_duration_seconds
        dur_str = f"{dur:.0f}s" if dur is not None else "—"
        turns_str = f"{sp.avg_turns:.1f}" if sp.avg_turns is not None else "—"
        rate = sp.success_rate
        if rate is None:
            rate_str = "—"
        elif rate >= 0.9:
            rate_str = f"[green]{rate:.0%}[/green]"
        elif rate >= 0.6:
            rate_str = f"[yellow]{rate:.0%}[/yellow]"
        else:
            rate_str = f"[red]{rate:.0%}[/red]"

        table.add_row(
            sp.specialist,
            str(sp.mission_count),
            f"${sp.total_cost_usd:.4f}",
            dur_str,
            turns_str,
            rate_str,
        )

    output.print_table(table)
    output.info(
        f"[dim]total {result.total_missions} missions · ${result.total_cost_usd:.4f} total cost[/dim]"
    )
