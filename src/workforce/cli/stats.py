"""`workforce stats` command.

Scans all project mission directories and renders aggregated statistics.
Supports per-specialist (default) and per-project (``--by-project``) pivots,
with Rich table, CSV, and JSON output modes.
"""

from __future__ import annotations

import csv
import dataclasses
import io
import json
import sys

import typer
from rich.table import Table

from workforce import output, paths
from workforce.stats import StatsResult, query_stats


def stats_command(
    since: str | None = typer.Option(
        None,
        "--since",
        metavar="DATE",
        help=(
            "Filter to missions started on or after DATE (ISO format, e.g. "
            "2026-05-01)."
        ),
    ),
    by_project: bool = typer.Option(
        False,
        "--by-project",
        help="Pivot the output to show one row per project instead of per specialist.",
    ),
    as_csv: bool = typer.Option(
        False,
        "--csv",
        help="Output CSV suitable for spreadsheet import.",
    ),
    as_json: bool = typer.Option(
        False,
        "--json",
        help="Output full StatsResult as JSON.",
    ),
) -> None:
    """Show mission statistics across all projects.

    By default prints a Rich table of per-specialist aggregates.  Use
    ``--by-project`` to pivot to per-project rows, ``--csv`` for
    machine-readable CSV, or ``--json`` for the full structured result.
    """
    paths.ensure_layout()
    result = query_stats(since_date=since)

    if as_json:
        _print_json(result)
        return

    if as_csv:
        _print_csv(result, by_project=by_project)
        return

    if by_project:
        _print_table_by_project(result)
    else:
        _print_table_by_specialist(result)

    _print_totals(result)


# ----- Output helpers -------------------------------------------------------


def _print_json(result: StatsResult) -> None:
    """Serialise *result* to JSON and write to stdout.

    Uses :func:`dataclasses.asdict` so the dataclass fields serialize cleanly;
    TypedDicts embedded in the dataclass are plain dicts at runtime and pass
    through as-is.
    """
    sys.stdout.write(json.dumps(dataclasses.asdict(result), indent=2))
    sys.stdout.write("\n")


def _print_csv(result: StatsResult, *, by_project: bool) -> None:
    """Write CSV to stdout.

    Columns depend on pivot mode:

    * per-specialist (default): specialist, missions, cost, avg_duration,
      success_rate
    * ``--by-project``: project, missions, cost, avg_duration, success_rate

    Args:
        result: The aggregated stats.
        by_project: When ``True``, emit per-project rows.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    if by_project:
        writer.writerow(["project", "missions", "cost", "success_rate"])
        for proj_id, ps in sorted(result.by_project.items()):
            writer.writerow([
                ps["project_name"],
                ps["missions"],
                f"{ps['total_cost']:.4f}",
                f"{ps['success_rate']:.2%}",
            ])
    else:
        writer.writerow(
            ["specialist", "missions", "cost", "avg_duration", "avg_turns",
             "success_rate"]
        )
        for spec, ss in sorted(result.by_specialist.items()):
            writer.writerow([
                spec,
                ss["missions"],
                f"{ss['total_cost']:.4f}",
                f"{ss['avg_duration']:.1f}",
                f"{ss['avg_turns']:.1f}",
                f"{ss['success_rate']:.2%}",
            ])
    sys.stdout.write(buf.getvalue())


def _print_table_by_specialist(result: StatsResult) -> None:
    """Render a Rich table with one row per specialist.

    Columns: specialist | missions | completed | failed | review_rejected |
             avg_cost | avg_duration | avg_turns | success_rate
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("specialist")
    table.add_column("missions", justify="right")
    table.add_column("completed", justify="right")
    table.add_column("failed", justify="right")
    table.add_column("rev.rejected", justify="right")
    table.add_column("avg cost", justify="right")
    table.add_column("avg dur(s)", justify="right")
    table.add_column("avg turns", justify="right")
    table.add_column("success %", justify="right")

    for spec, ss in sorted(result.by_specialist.items()):
        table.add_row(
            spec,
            str(ss["missions"]),
            str(ss["completed"]),
            str(ss["failed"]),
            str(ss["review_rejected"]),
            f"${ss['avg_cost']:.4f}",
            f"{ss['avg_duration']:.1f}",
            f"{ss['avg_turns']:.1f}",
            f"{ss['success_rate']:.1%}",
        )

    output.print_table(table)


def _print_table_by_project(result: StatsResult) -> None:
    """Render a Rich table with one row per project.

    Columns: project | missions | completed | total_cost | success_rate
    """
    table = Table(show_header=True, header_style="bold")
    table.add_column("project")
    table.add_column("missions", justify="right")
    table.add_column("completed", justify="right")
    table.add_column("total cost", justify="right")
    table.add_column("success %", justify="right")

    for _proj_id, ps in sorted(
        result.by_project.items(), key=lambda kv: kv[1]["project_name"]
    ):
        table.add_row(
            ps["project_name"],
            str(ps["missions"]),
            str(ps["completed"]),
            f"${ps['total_cost']:.4f}",
            f"{ps['success_rate']:.1%}",
        )

    output.print_table(table)


def _print_totals(result: StatsResult) -> None:
    """Print the global totals line below the main table.

    Args:
        result: The aggregated stats to summarise.
    """
    t = result.totals
    label = f"since filter applied" if result.filtered_count != t["missions"] else ""
    output.info(
        f"[dim]total missions: {t['missions']}  "
        f"completed: {t['completed']}  "
        f"total cost: ${t['total_cost']:.4f}  "
        f"success: {t['success_rate']:.1%}"
        + (f"  ({label})" if label else "")
        + "[/dim]"
    )
