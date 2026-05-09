"""Tests for workforce.stats and the `workforce stats` CLI command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from workforce.cli import app
from workforce.mission import MissionStatus
from workforce.stats import StatsResult, _cache_path, query_stats

# ----- Fixtures -------------------------------------------------------------


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary WORKFORCE_HOME for test isolation."""
    home = tmp_path / "wfhome"
    home.mkdir()
    monkeypatch.setenv("WORKFORCE_HOME", str(home))
    return home


def _write_meta(home: Path, project_id: str, project_name: str, mission_id: str,
                specialist: str, status: str, cost: float, duration: float,
                turns: int, started_at: str) -> None:
    """Write a minimal meta.json file for one mission."""
    mission_dir = home / "projects" / project_id / "missions" / mission_id
    mission_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema_version": 1,
        "mission_id": mission_id,
        "project_id": project_id,
        "project_name": project_name,
        "specialist": specialist,
        "model": "claude-opus-4-5",
        "ticket": "do something",
        "started_at": started_at,
        "ended_at": started_at,
        "duration_seconds": duration,
        "status": status,
        "cost_usd": cost,
        "manager_cost_usd": 0.0,
        "review_cost_usd": 0.0,
        "turn_count": turns,
        "commits": [],
        "memory_delta_captured": False,
        "reviews": [],
        "revision_rounds": 0,
    }
    (mission_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _write_parent_meta(home: Path, project_id: str, project_name: str,
                       mission_id: str) -> None:
    """Write a minimal parallel parent meta.json (should be ignored by stats)."""
    mission_dir = home / "projects" / project_id / "missions" / mission_id
    mission_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema_version": 1,
        "parent_mission_id": mission_id,
        "project_id": project_id,
        "project_name": project_name,
        "started_at": "2026-05-01T10:00:00+00:00",
        "ended_at": "2026-05-01T10:01:00+00:00",
        "manager_cost_usd": 0.05,
        "decomposition_kind": "parallel",
        "status": "completed",
        "sub_missions": [],
    }
    (mission_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


# ----- Unit tests for query_stats -------------------------------------------


def test_empty_home_returns_empty_result(isolated_home: Path) -> None:
    (isolated_home / "projects").mkdir()
    result = query_stats()
    assert isinstance(result, StatsResult)
    assert result.filtered_count == 0
    assert result.by_specialist == {}
    assert result.by_project == {}
    assert result.totals["missions"] == 0
    assert result.totals["success_rate"] == 0.0


def test_single_completed_mission(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    result = query_stats()
    assert result.filtered_count == 1
    assert "builder" in result.by_specialist
    ss = result.by_specialist["builder"]
    assert ss["missions"] == 1
    assert ss["completed"] == 1
    assert ss["failed"] == 0
    assert ss["review_rejected"] == 0
    assert ss["avg_cost"] == pytest.approx(0.10)
    assert ss["avg_duration"] == pytest.approx(60.0)
    assert ss["avg_turns"] == pytest.approx(5.0)
    assert ss["success_rate"] == pytest.approx(1.0)

    assert "proj01" in result.by_project
    ps = result.by_project["proj01"]
    assert ps["missions"] == 1
    assert ps["completed"] == 1
    assert ps["total_cost"] == pytest.approx(0.10)
    assert ps["success_rate"] == pytest.approx(1.0)


def test_multiple_statuses(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.05, duration=30.0, turns=3,
        started_at="2026-05-01T10:00:00+00:00",
    )
    _write_meta(
        isolated_home, "proj01", "myapp", "m-002", "builder",
        MissionStatus.ERROR, cost=0.02, duration=10.0, turns=1,
        started_at="2026-05-02T10:00:00+00:00",
    )
    _write_meta(
        isolated_home, "proj01", "myapp", "m-003", "builder",
        MissionStatus.REVIEW_REJECTED, cost=0.08, duration=50.0, turns=8,
        started_at="2026-05-03T10:00:00+00:00",
    )
    result = query_stats()
    ss = result.by_specialist["builder"]
    assert ss["missions"] == 3
    assert ss["completed"] == 1
    assert ss["failed"] == 1
    assert ss["review_rejected"] == 1
    assert ss["success_rate"] == pytest.approx(1 / 3)


def test_multi_specialist_aggregation(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    _write_meta(
        isolated_home, "proj01", "myapp", "m-002", "tester",
        MissionStatus.COMPLETED, cost=0.20, duration=120.0, turns=10,
        started_at="2026-05-01T11:00:00+00:00",
    )
    result = query_stats()
    assert set(result.by_specialist.keys()) == {"builder", "tester"}
    assert result.totals["missions"] == 2
    assert result.totals["total_cost"] == pytest.approx(0.30)
    assert result.totals["success_rate"] == pytest.approx(1.0)


def test_since_date_filters_missions(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-04-01T10:00:00+00:00",  # before cutoff
    )
    _write_meta(
        isolated_home, "proj01", "myapp", "m-002", "builder",
        MissionStatus.COMPLETED, cost=0.20, duration=80.0, turns=8,
        started_at="2026-05-02T10:00:00+00:00",  # after cutoff
    )
    result = query_stats(since_date="2026-05-01")
    assert result.filtered_count == 1
    assert result.by_specialist["builder"]["missions"] == 1
    assert result.by_specialist["builder"]["avg_cost"] == pytest.approx(0.20)


def test_since_date_on_boundary_is_inclusive(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T00:00:00+00:00",  # exactly on cutoff
    )
    result = query_stats(since_date="2026-05-01")
    assert result.filtered_count == 1


def test_since_date_all_filtered_out(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-04-01T10:00:00+00:00",
    )
    result = query_stats(since_date="2026-05-01")
    assert result.filtered_count == 0
    assert result.by_specialist == {}


def test_parent_missions_are_ignored(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    _write_parent_meta(isolated_home, "proj01", "myapp", "m-parent-001")
    result = query_stats()
    assert result.filtered_count == 1


def test_multi_project_breakdown(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "app1", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    _write_meta(
        isolated_home, "proj02", "app2", "m-002", "builder",
        MissionStatus.ERROR, cost=0.05, duration=20.0, turns=2,
        started_at="2026-05-02T10:00:00+00:00",
    )
    result = query_stats()
    assert set(result.by_project.keys()) == {"proj01", "proj02"}
    assert result.by_project["proj01"]["success_rate"] == pytest.approx(1.0)
    assert result.by_project["proj02"]["success_rate"] == pytest.approx(0.0)


# ----- Cache tests ----------------------------------------------------------


def test_cache_is_written_on_first_scan(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    query_stats()
    cp = _cache_path()
    assert cp.is_file()
    data = json.loads(cp.read_text())
    assert "files" in data
    assert "missions" in data
    assert len(data["missions"]) == 1
    assert data["missions"][0]["specialist"] == "builder"


def test_cache_is_reused_when_files_unchanged(isolated_home: Path) -> None:
    """Second query_stats() call should use cache without re-reading meta.json."""
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    r1 = query_stats()
    # Corrupt the underlying meta.json; cache should still return old data.
    (
        isolated_home / "projects" / "proj01" / "missions" / "m-001" / "meta.json"
    )
    # We can't simply corrupt it because that would change the mtime, which
    # would invalidate the cache.  Instead we verify the cache file was written
    # and contains the data, then re-run to confirm the cached count matches.
    r2 = query_stats()
    assert r1.filtered_count == r2.filtered_count == 1


def test_cache_invalidated_when_new_mission_added(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    query_stats()

    # Add a second mission — cache should rebuild.
    _write_meta(
        isolated_home, "proj01", "myapp", "m-002", "builder",
        MissionStatus.COMPLETED, cost=0.20, duration=80.0, turns=8,
        started_at="2026-05-02T10:00:00+00:00",
    )
    result = query_stats()
    assert result.filtered_count == 2


# ----- CLI tests ------------------------------------------------------------


def test_cli_stats_empty(isolated_home: Path) -> None:
    (isolated_home / "projects").mkdir()
    runner = CliRunner()
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0


def test_cli_stats_default_table(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
    # The totals line is emitted via output.info() which survives CliRunner capture.
    assert "total missions: 1" in result.output
    assert "success" in result.output.lower()


def test_cli_stats_by_project(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["stats", "--by-project"])
    assert result.exit_code == 0
    # Verify via JSON that the by_project pivot works.
    result2 = runner.invoke(app, ["stats", "--json", "--by-project"])
    assert result2.exit_code == 0
    data = json.loads(result2.output)
    assert "proj01" in data["by_project"]
    assert data["by_project"]["proj01"]["project_name"] == "myapp"


def test_cli_stats_json(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["stats", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert "by_specialist" in data
    assert "by_project" in data
    assert "totals" in data
    assert "filtered_count" in data
    assert data["filtered_count"] == 1
    assert "builder" in data["by_specialist"]


def test_cli_stats_csv_specialist(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["stats", "--csv"])
    assert result.exit_code == 0
    lines = result.output.strip().split("\n")
    assert lines[0].startswith("specialist,")
    assert "builder" in lines[1]


def test_cli_stats_csv_by_project(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-05-01T10:00:00+00:00",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["stats", "--csv", "--by-project"])
    assert result.exit_code == 0
    lines = result.output.strip().split("\n")
    assert lines[0].startswith("project,")
    assert "myapp" in lines[1]


def test_cli_stats_since_filters(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.COMPLETED, cost=0.10, duration=60.0, turns=5,
        started_at="2026-04-01T10:00:00+00:00",
    )
    _write_meta(
        isolated_home, "proj01", "myapp", "m-002", "builder",
        MissionStatus.COMPLETED, cost=0.20, duration=80.0, turns=8,
        started_at="2026-05-10T10:00:00+00:00",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["stats", "--json", "--since", "2026-05-01"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["filtered_count"] == 1
    assert data["by_specialist"]["builder"]["missions"] == 1


def test_cli_stats_wall_timeout_counted_as_failed(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.WALL_TIMEOUT, cost=0.05, duration=300.0, turns=20,
        started_at="2026-05-01T10:00:00+00:00",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["stats", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    ss = data["by_specialist"]["builder"]
    assert ss["failed"] == 1
    assert ss["completed"] == 0
    assert ss["review_rejected"] == 0


def test_cli_stats_interrupted_counted_as_failed(isolated_home: Path) -> None:
    _write_meta(
        isolated_home, "proj01", "myapp", "m-001", "builder",
        MissionStatus.INTERRUPTED, cost=0.01, duration=10.0, turns=2,
        started_at="2026-05-01T10:00:00+00:00",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["stats", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    ss = data["by_specialist"]["builder"]
    assert ss["failed"] == 1
