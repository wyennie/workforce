"""Aggregate statistics across all missions in the Workforce home directory.

Scans every ``meta.json`` under ``~/.workforce/projects/*/missions/`` and
returns a :class:`StatsResult` suitable for display or JSON export.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from workforce import paths
from workforce.mission import MissionMeta, MissionStatus


@dataclass
class SpecialistStats:
    """Per-specialist aggregated statistics."""

    specialist: str
    mission_count: int = 0
    completed: int = 0
    failed: int = 0  # error + wall_timeout + review_rejected
    interrupted: int = 0
    total_cost_usd: float = 0.0
    total_duration_seconds: float = 0.0
    total_turns: int = 0
    reviewer_rejections: int = 0  # missions whose final status == review_rejected

    @property
    def success_rate(self) -> float | None:
        """Fraction of missions that completed. None if no missions."""
        if self.mission_count == 0:
            return None
        return self.completed / self.mission_count

    @property
    def avg_duration_seconds(self) -> float | None:
        """Mean duration across recorded missions. None if no missions."""
        if self.mission_count == 0:
            return None
        return self.total_duration_seconds / self.mission_count

    @property
    def avg_turns(self) -> float | None:
        """Mean turn count. None if no missions."""
        if self.mission_count == 0:
            return None
        return self.total_turns / self.mission_count


@dataclass
class StatsResult:
    """Top-level statistics returned by :func:`query_stats`."""

    by_specialist: dict[str, SpecialistStats] = field(default_factory=dict)
    # missions by (project_id, specialist) -> cost_usd total
    by_project: dict[str, float] = field(default_factory=dict)
    total_missions: int = 0
    total_cost_usd: float = 0.0


def _load_meta(path: Path) -> MissionMeta | None:
    """Attempt to parse a meta.json as a single-mission MissionMeta.

    Returns None for parallel parent metas (they have ``parent_mission_id``) or
    any file that cannot be parsed.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if "parent_mission_id" in data:
        return None  # parallel meta; skip
    try:
        return MissionMeta.model_validate(data)
    except Exception:
        return None


def query_stats(
    *,
    project_id: str | None = None,
    specialist_name: str | None = None,
    since_date: str | None = None,
) -> StatsResult:
    """Scan all meta.json files and return aggregated :class:`StatsResult`.

    Args:
        project_id: If given, restrict to this project.
        specialist_name: If given, restrict to this specialist.
        since_date: ISO date string (``YYYY-MM-DD``). Only missions whose
            ``started_at`` is on or after this date are included.

    Returns:
        A :class:`StatsResult` with per-specialist and per-project totals.
    """
    projects_dir = paths.projects_dir()
    if not projects_dir.is_dir():
        return StatsResult()

    result = StatsResult()

    project_dirs: list[Path]
    if project_id is not None:
        target = projects_dir / project_id
        project_dirs = [target] if target.is_dir() else []
    else:
        project_dirs = [p for p in sorted(projects_dir.iterdir()) if p.is_dir()]

    for proj_dir in project_dirs:
        pid = proj_dir.name
        missions_dir = proj_dir / "missions"
        if not missions_dir.is_dir():
            continue
        for mission_dir in sorted(missions_dir.iterdir()):
            if not mission_dir.is_dir():
                continue
            meta_path = mission_dir / "meta.json"
            if not meta_path.is_file():
                continue
            meta = _load_meta(meta_path)
            if meta is None:
                continue

            # Apply filters
            if specialist_name is not None and meta.specialist != specialist_name:
                continue
            if since_date is not None and meta.started_at < since_date:
                continue

            # Accumulate
            result.total_missions += 1
            result.total_cost_usd += meta.cost_usd

            # Per-project cost
            result.by_project[pid] = result.by_project.get(pid, 0.0) + meta.cost_usd

            # Per-specialist stats
            sp_key = meta.specialist
            if sp_key not in result.by_specialist:
                result.by_specialist[sp_key] = SpecialistStats(specialist=sp_key)
            sp = result.by_specialist[sp_key]
            sp.mission_count += 1
            sp.total_cost_usd += meta.cost_usd
            sp.total_duration_seconds += meta.duration_seconds
            sp.total_turns += meta.turn_count

            if meta.status == MissionStatus.COMPLETED:
                sp.completed += 1
            elif meta.status == MissionStatus.INTERRUPTED:
                sp.interrupted += 1
            elif meta.status == MissionStatus.REVIEW_REJECTED:
                sp.failed += 1
                sp.reviewer_rejections += 1
            else:
                # error, wall_timeout, etc.
                sp.failed += 1

    return result
