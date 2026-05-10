"""Project budget checking.

Scans the current month's mission artifacts and compares their cost against
per-project budget limits set in the Project model.

Usage::

    from workforce.budget import check_budget
    from workforce.project import Project

    result = check_budget(project_id, project)
    if not result.allowed:
        output.die(f"budget: {result.reason}")
    if result.warning:
        output.warn(f"budget: {result.warning}")
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import NamedTuple

from workforce import paths
from workforce.project import Project


class BudgetCheckResult(NamedTuple):
    """Result of a budget check.

    Attributes:
        allowed: False if the project's monthly limit has been reached and the
            new mission should be blocked.
        warning: Non-None string if spend is above the alert threshold but
            below the hard limit. The caller should display this to the user
            before dispatching.
        reason: Non-None string when ``allowed`` is False, describing why the
            mission is blocked (e.g. "monthly budget exhausted: $4.90 of $5.00
            used").
    """

    allowed: bool
    warning: str | None
    reason: str | None


def check_budget(
    project_id: str,
    project_config: Project,
    new_cost_estimate: float = 0.0,
) -> BudgetCheckResult:
    """Check whether a new mission can proceed given the project's budget limits.

    Scans ``meta.json`` files for *project_id* in the current calendar month and
    sums ``cost_usd`` from ``completed`` and ``error`` missions. Parallel parent
    metas (identified by a ``parent_mission_id`` key) are skipped to avoid
    double-counting child costs.

    ``new_cost_estimate`` is added to the current spend so the check reflects
    the projected total if the new mission runs at the estimated cost.

    Returns a `BudgetCheckResult`:
    - ``allowed=False`` when the projected spend meets or exceeds the monthly
      limit.
    - ``allowed=True, warning=<msg>`` when spend is at or above
      ``alert_threshold_pct`` percent of the limit.
    - ``allowed=True, warning=None, reason=None`` when no limit is configured
      or spend is safely below the threshold.

    Args:
        project_id: The 12-hex project identifier.
        project_config: The Project model containing budget settings.
        new_cost_estimate: Estimated cost of the mission about to be launched,
            in USD.  Defaults to 0.0 (no estimate).

    Returns:
        A BudgetCheckResult describing whether dispatch is allowed.
    """
    limit = project_config.monthly_limit_usd
    if limit is None or limit <= 0:
        # No budget configured — always allowed with no warning.
        return BudgetCheckResult(allowed=True, warning=None, reason=None)

    current_spend = _sum_monthly_cost(project_id)
    projected_spend = current_spend + new_cost_estimate
    threshold_pct = project_config.alert_threshold_pct

    if projected_spend >= limit:
        return BudgetCheckResult(
            allowed=False,
            warning=None,
            reason=(
                f"monthly budget exhausted: ${projected_spend:.2f} of "
                f"${limit:.2f} used this month"
            ),
        )

    pct_used = (projected_spend / limit) * 100
    if pct_used >= threshold_pct:
        return BudgetCheckResult(
            allowed=True,
            warning=(
                f"{pct_used:.0f}% of monthly budget used "
                f"(${projected_spend:.2f} of ${limit:.2f})"
            ),
            reason=None,
        )

    return BudgetCheckResult(allowed=True, warning=None, reason=None)


def _sum_monthly_cost(project_id: str) -> float:
    """Sum ``cost_usd`` from completed/error missions in the current month.

    Args:
        project_id: The 12-hex project identifier.

    Returns:
        Total USD cost of qualifying missions started in the current calendar
        month.  Returns 0.0 if the missions directory doesn't exist or contains
        no qualifying missions.
    """
    missions_dir: Path = paths.project_dir(project_id) / "missions"
    if not missions_dir.is_dir():
        return 0.0

    now = dt.datetime.now(dt.UTC)
    current_year_month = (now.year, now.month)
    total = 0.0

    for mission_dir in missions_dir.iterdir():
        if not mission_dir.is_dir():
            continue
        meta_path = mission_dir / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            data: dict[str, object] = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        # Skip parallel parent metas — their cost_usd aggregates child costs,
        # which are already counted individually in each sub-mission's meta.json.
        if "parent_mission_id" in data:
            continue

        status = data.get("status", "")
        if status not in {"completed", "error"}:
            continue

        started_at = data.get("started_at", "")
        if not isinstance(started_at, str):
            continue
        try:
            # meta.json timestamps use "Z" suffix (not "+00:00")
            started = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            continue

        if (started.year, started.month) != current_year_month:
            continue

        cost = data.get("cost_usd", 0.0)
        if isinstance(cost, (int, float)):
            total += cost

    return total
