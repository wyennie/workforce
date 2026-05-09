"""Mission statistics: scanning, aggregation, and caching.

Scans all project mission directories, extracts key fields from meta.json
files, and aggregates them by specialist and project.  A cache at
``~/.workforce/stats-cache.json`` stores the raw per-mission records plus the
file signatures used for invalidation; the cache is rebuilt whenever any file's
mtime changes or new files appear.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from workforce import paths
from workforce.mission import MissionStatus

# ----- Per-record store (raw, before filtering) -----------------------------


class _MissionRecord(TypedDict):
    """Minimal per-mission data stored in the on-disk cache."""

    specialist: str
    project_id: str
    project_name: str
    status: str
    cost_usd: float
    duration_seconds: float
    turn_count: int
    started_at: str  # ISO-8601 UTC string


# ----- Aggregated result types ---------------------------------------------


class SpecialistStats(TypedDict):
    """Aggregated per-specialist statistics."""

    missions: int
    completed: int
    failed: int
    review_rejected: int
    total_cost: float
    avg_cost: float
    avg_duration: float
    avg_turns: float
    success_rate: float


class ProjectStats(TypedDict):
    """Aggregated per-project statistics."""

    project_name: str
    missions: int
    completed: int
    total_cost: float
    success_rate: float


class Totals(TypedDict):
    """Global totals across all filtered missions."""

    missions: int
    completed: int
    total_cost: float
    success_rate: float


@dataclass
class StatsResult:
    """Full aggregated stats result returned by :func:`query_stats`.

    Attributes:
        by_specialist: Stats keyed by specialist name.
        by_project: Stats keyed by project id.
        totals: Cross-project aggregate totals.
        filtered_count: Number of missions that matched the filter (same as
            totals["missions"]; exposed as a top-level field for convenience).
    """

    by_specialist: dict[str, SpecialistStats]
    by_project: dict[str, ProjectStats]
    totals: Totals
    filtered_count: int


# ----- Cache ----------------------------------------------------------------


_CACHE_FILENAME = "stats-cache.json"


def _cache_path() -> Path:
    """Return the path to the on-disk stats cache file."""
    return paths.home() / _CACHE_FILENAME


def _collect_meta_paths() -> list[Path]:
    """Return sorted paths to all meta.json files under the projects directory.

    Skips missing or non-directory entries at each level so the function
    returns cleanly on a fresh or empty home.
    """
    pdir = paths.projects_dir()
    if not pdir.is_dir():
        return []
    result: list[Path] = []
    for project_dir in sorted(pdir.iterdir()):
        if not project_dir.is_dir():
            continue
        missions_dir = project_dir / "missions"
        if not missions_dir.is_dir():
            continue
        for mission_dir in sorted(missions_dir.iterdir()):
            if not mission_dir.is_dir():
                continue
            meta = mission_dir / "meta.json"
            if meta.is_file():
                result.append(meta)
    return result


def _load_mission_record(meta_path: Path) -> _MissionRecord | None:
    """Parse a single meta.json file into a lightweight record.

    Returns None for parallel parent metas (identified by the
    ``parent_mission_id`` key) and for unparseable files.
    """
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # Parallel parent metas have a different shape; skip them.
    if "parent_mission_id" in data:
        return None
    required = ("specialist", "project_id", "project_name", "status", "started_at")
    if not all(k in data for k in required):
        return None
    return _MissionRecord(
        specialist=data["specialist"],
        project_id=data["project_id"],
        project_name=data["project_name"],
        status=data["status"],
        cost_usd=float(data.get("cost_usd", 0.0)),
        duration_seconds=float(data.get("duration_seconds", 0.0)),
        turn_count=int(data.get("turn_count", 0)),
        started_at=data["started_at"],
    )


def _load_cache() -> tuple[list[dict], list[_MissionRecord]] | None:
    """Load the cache from disk.

    Returns:
        A ``(files, missions)`` tuple where *files* is the list of
        ``{"path": str, "mtime": float}`` entries written at last scan, and
        *missions* is the raw record list.  Returns ``None`` if the cache is
        absent or malformed.
    """
    cp = _cache_path()
    if not cp.is_file():
        return None
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        return data["files"], data["missions"]
    except (OSError, ValueError, KeyError):
        return None


def _save_cache(files: list[dict], missions: list[_MissionRecord]) -> None:
    """Atomically write the stats cache to disk.

    Args:
        files: List of ``{"path": str, "mtime": float}`` dicts representing
            the files scanned.
        missions: Flattened list of mission records derived from those files.
    """
    cp = _cache_path()
    cp.parent.mkdir(parents=True, exist_ok=True)
    tmp = cp.with_name(cp.name + ".tmp")
    tmp.write_text(
        json.dumps({"files": files, "missions": missions}),
        encoding="utf-8",
    )
    os.replace(tmp, cp)


def _get_missions_with_cache() -> list[_MissionRecord]:
    """Return all mission records, rebuilding the cache only when files change.

    Builds a signature list of ``(path, mtime)`` for every meta.json found on
    disk.  If this matches the cached signature list exactly, the cached records
    are returned as-is.  Otherwise the files are re-read, aggregated, and the
    cache is updated.

    Returns:
        List of lightweight mission records covering every project.
    """
    meta_paths = _collect_meta_paths()

    current_files: list[dict] = []
    for p in meta_paths:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        current_files.append({"path": str(p), "mtime": mtime})

    cached = _load_cache()
    if cached is not None:
        cached_files, cached_missions = cached
        if cached_files == current_files:
            return cached_missions

    missions: list[_MissionRecord] = []
    for p in meta_paths:
        rec = _load_mission_record(p)
        if rec is not None:
            missions.append(rec)

    _save_cache(current_files, missions)
    return missions


# ----- Aggregation ----------------------------------------------------------


def _parse_started_at(started_at: str) -> dt.datetime | None:
    """Parse an ISO-8601 started_at string to a timezone-aware datetime.

    Args:
        started_at: ISO-8601 string, optionally with timezone suffix.

    Returns:
        An aware ``datetime`` (UTC if no timezone in the string), or ``None``
        if parsing fails.
    """
    try:
        parsed = dt.datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.UTC)
        return parsed
    except (ValueError, AttributeError):
        return None


def query_stats(since_date: str | None = None) -> StatsResult:
    """Aggregate mission statistics across all projects.

    Reads from the on-disk cache when file signatures are unchanged; otherwise
    rescans and refreshes the cache.

    Args:
        since_date: Optional ISO date string (e.g. ``'2026-05-01'``).  When
            given, only missions whose ``started_at`` falls on or after this
            date are included.

    Returns:
        A :class:`StatsResult` with per-specialist, per-project, and global
        totals, plus the count of missions that matched the filter.
    """
    missions = _get_missions_with_cache()

    # Apply optional time-window filter.
    if since_date is not None:
        try:
            cutoff = dt.datetime.fromisoformat(since_date).replace(
                tzinfo=dt.UTC
            )
        except ValueError:
            cutoff = None  # invalid date → no filter
        if cutoff is not None:
            filtered: list[_MissionRecord] = []
            for m in missions:
                ts = _parse_started_at(m["started_at"])
                if ts is not None and ts >= cutoff:
                    filtered.append(m)
            missions = filtered

    filtered_count = len(missions)

    # Per-specialist accumulator.
    spec_acc: dict[str, dict] = {}
    # Per-project accumulator.
    proj_acc: dict[str, dict] = {}

    total_missions = 0
    total_cost = 0.0
    total_completed = 0

    for m in missions:
        total_missions += 1
        total_cost += m["cost_usd"]

        status = m["status"]
        is_completed = status == MissionStatus.COMPLETED
        is_failed = status in (
            MissionStatus.ERROR,
            MissionStatus.WALL_TIMEOUT,
            MissionStatus.INTERRUPTED,
        )
        is_review_rejected = status == MissionStatus.REVIEW_REJECTED
        if is_completed:
            total_completed += 1

        # ----- specialist -------------------------------------------------
        spec = m["specialist"]
        if spec not in spec_acc:
            spec_acc[spec] = dict(
                missions=0,
                completed=0,
                failed=0,
                review_rejected=0,
                total_cost=0.0,
                total_duration=0.0,
                total_turns=0,
            )
        sa = spec_acc[spec]
        sa["missions"] += 1
        sa["total_cost"] += m["cost_usd"]
        sa["total_duration"] += m["duration_seconds"]
        sa["total_turns"] += m["turn_count"]
        if is_completed:
            sa["completed"] += 1
        elif is_failed:
            sa["failed"] += 1
        elif is_review_rejected:
            sa["review_rejected"] += 1

        # ----- project ----------------------------------------------------
        proj_id = m["project_id"]
        if proj_id not in proj_acc:
            proj_acc[proj_id] = dict(
                project_name=m["project_name"],
                missions=0,
                completed=0,
                total_cost=0.0,
            )
        pa = proj_acc[proj_id]
        pa["missions"] += 1
        pa["total_cost"] += m["cost_usd"]
        if is_completed:
            pa["completed"] += 1

    # Build final by_specialist.
    by_specialist: dict[str, SpecialistStats] = {}
    for spec, sa in spec_acc.items():
        n = sa["missions"]
        by_specialist[spec] = SpecialistStats(
            missions=n,
            completed=sa["completed"],
            failed=sa["failed"],
            review_rejected=sa["review_rejected"],
            total_cost=sa["total_cost"],
            avg_cost=sa["total_cost"] / n if n else 0.0,
            avg_duration=sa["total_duration"] / n if n else 0.0,
            avg_turns=sa["total_turns"] / n if n else 0.0,
            success_rate=sa["completed"] / n if n else 0.0,
        )

    # Build final by_project.
    by_project: dict[str, ProjectStats] = {}
    for proj_id, pa in proj_acc.items():
        n = pa["missions"]
        by_project[proj_id] = ProjectStats(
            project_name=pa["project_name"],
            missions=n,
            completed=pa["completed"],
            total_cost=pa["total_cost"],
            success_rate=pa["completed"] / n if n else 0.0,
        )

    totals: Totals = Totals(
        missions=total_missions,
        completed=total_completed,
        total_cost=total_cost,
        success_rate=total_completed / total_missions if total_missions else 0.0,
    )

    return StatsResult(
        by_specialist=by_specialist,
        by_project=by_project,
        totals=totals,
        filtered_count=filtered_count,
    )
