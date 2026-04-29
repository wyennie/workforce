"""Tests for workforce.stats and workforce.cli.stats."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from workforce.cli import app
from workforce.mission import MissionStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_meta(missions_dir: Path, mission_id: str, **kwargs: object) -> None:
    """Write a synthetic meta.json under *missions_dir*/<mission_id>/meta.json."""
    d = missions_dir / mission_id
    d.mkdir(parents=True, exist_ok=True)
    defaults = dict(
        schema_version=1,
        mission_id=mission_id,
        project_id="proj1",
        project_name="TestProject",
        specialist="aria",
        model="claude-3-5-sonnet",
        ticket="Do something",
        branch="workforce/" + mission_id,
        worktree_path=None,
        base_sha=None,
        started_at="2026-05-10T12:00:00Z",
        ended_at="2026-05-10T12:01:30Z",
        duration_seconds=90.0,
        status=MissionStatus.COMPLETED,
        error_detail=None,
        cost_usd=0.05,
        manager_cost_usd=0.0,
        review_cost_usd=0.0,
        turn_count=10,
        commits=[],
        memory_delta_captured=True,
        reviews=[],
        revision_rounds=0,
    )
    defaults.update(kwargs)
    (d / "meta.json").write_text(json.dumps(defaults), encoding="utf-8")


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set WORKFORCE_HOME to a clean temp directory and return it."""
    monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
    return tmp_path


# ---------------------------------------------------------------------------
# Unit tests for workforce.stats.query_stats
# ---------------------------------------------------------------------------


def test_empty_home_returns_empty(isolated_home: Path) -> None:
    from workforce.stats import query_stats

    result = query_stats()
    assert result.total_missions == 0
    assert result.by_specialist == {}
    assert result.by_project == {}


def test_single_completed_mission(isolated_home: Path) -> None:
    from workforce.stats import query_stats

    missions_dir = isolated_home / "projects" / "proj1" / "missions"
    _write_meta(missions_dir, "m-0001")

    result = query_stats()
    assert result.total_missions == 1
    assert "aria" in result.by_specialist
    sp = result.by_specialist["aria"]
    assert sp.mission_count == 1
    assert sp.completed == 1
    assert sp.failed == 0
    assert sp.total_cost_usd == pytest.approx(0.05)
    assert sp.success_rate == pytest.approx(1.0)


def test_multiple_missions_aggregated(isolated_home: Path) -> None:
    from workforce.stats import query_stats

    missions_dir = isolated_home / "projects" / "proj1" / "missions"
    _write_meta(missions_dir, "m-0001", cost_usd=0.10, status=MissionStatus.COMPLETED)
    _write_meta(missions_dir, "m-0002", cost_usd=0.20, status=MissionStatus.ERROR)
    _write_meta(missions_dir, "m-0003", cost_usd=0.05, status=MissionStatus.COMPLETED)

    result = query_stats()
    assert result.total_missions == 3
    sp = result.by_specialist["aria"]
    assert sp.mission_count == 3
    assert sp.completed == 2
    assert sp.failed == 1
    assert sp.total_cost_usd == pytest.approx(0.35)
    assert sp.success_rate == pytest.approx(2 / 3)


def test_reviewer_rejection_counted(isolated_home: Path) -> None:
    from workforce.stats import query_stats

    missions_dir = isolated_home / "projects" / "proj1" / "missions"
    _write_meta(missions_dir, "m-0001", status=MissionStatus.REVIEW_REJECTED)

    result = query_stats()
    sp = result.by_specialist["aria"]
    assert sp.reviewer_rejections == 1
    assert sp.failed == 1


def test_filter_by_specialist(isolated_home: Path) -> None:
    from workforce.stats import query_stats

    missions_dir = isolated_home / "projects" / "proj1" / "missions"
    _write_meta(missions_dir, "m-0001", specialist="aria")
    _write_meta(missions_dir, "m-0002", specialist="ben")

    result = query_stats(specialist_name="aria")
    assert result.total_missions == 1
    assert "aria" in result.by_specialist
    assert "ben" not in result.by_specialist


def test_filter_by_since_date(isolated_home: Path) -> None:
    from workforce.stats import query_stats

    missions_dir = isolated_home / "projects" / "proj1" / "missions"
    _write_meta(missions_dir, "m-0001", started_at="2026-04-01T12:00:00Z")
    _write_meta(missions_dir, "m-0002", started_at="2026-05-10T12:00:00Z")

    result = query_stats(since_date="2026-05-01")
    assert result.total_missions == 1


def test_filter_by_project_id(isolated_home: Path) -> None:
    from workforce.stats import query_stats

    m1 = isolated_home / "projects" / "proj1" / "missions"
    m2 = isolated_home / "projects" / "proj2" / "missions"
    _write_meta(m1, "m-0001", project_id="proj1")
    _write_meta(m2, "m-0002", project_id="proj2")

    result = query_stats(project_id="proj1")
    assert result.total_missions == 1
    assert "proj1" in result.by_project
    assert "proj2" not in result.by_project


def test_skips_parallel_meta(isolated_home: Path) -> None:
    """Parallel parent metas (parent_mission_id key) must not be counted."""
    from workforce.stats import query_stats

    mission_dir = isolated_home / "projects" / "proj1" / "missions" / "m-0001"
    mission_dir.mkdir(parents=True)
    (mission_dir / "meta.json").write_text(
        json.dumps({"parent_mission_id": "m-0001", "status": "completed"}),
        encoding="utf-8",
    )

    result = query_stats()
    assert result.total_missions == 0


def test_skips_malformed_meta(isolated_home: Path) -> None:
    mission_dir = isolated_home / "projects" / "proj1" / "missions" / "m-0001"
    mission_dir.mkdir(parents=True)
    (mission_dir / "meta.json").write_text("not valid json {{{", encoding="utf-8")

    from workforce.stats import query_stats

    result = query_stats()
    assert result.total_missions == 0


def test_avg_duration_and_turns(isolated_home: Path) -> None:
    from workforce.stats import query_stats

    missions_dir = isolated_home / "projects" / "proj1" / "missions"
    _write_meta(missions_dir, "m-0001", duration_seconds=60.0, turn_count=5)
    _write_meta(missions_dir, "m-0002", duration_seconds=120.0, turn_count=15)

    result = query_stats()
    sp = result.by_specialist["aria"]
    assert sp.avg_duration_seconds == pytest.approx(90.0)
    assert sp.avg_turns == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Integration tests for the CLI command
# ---------------------------------------------------------------------------


def test_stats_cli_no_missions(isolated_home: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
    assert "no missions" in result.output


def test_stats_cli_table_output(isolated_home: Path) -> None:
    missions_dir = isolated_home / "projects" / "proj1" / "missions"
    _write_meta(missions_dir, "m-0001", cost_usd=0.12)

    runner = CliRunner()
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
    assert "aria" in result.output
    assert "0.1200" in result.output


def test_stats_cli_json_output(isolated_home: Path) -> None:
    missions_dir = isolated_home / "projects" / "proj1" / "missions"
    _write_meta(missions_dir, "m-0001", cost_usd=0.07, turn_count=8)

    runner = CliRunner()
    result = runner.invoke(app, ["stats", "--json"])
    assert result.exit_code == 0
    # Strip Rich markup and find JSON in output
    raw = result.output.strip()
    data = json.loads(raw)
    assert data["total_missions"] == 1
    assert data["total_cost_usd"] == pytest.approx(0.07)
    assert len(data["by_specialist"]) == 1
    assert data["by_specialist"][0]["specialist"] == "aria"
    assert data["by_specialist"][0]["avg_turns"] == pytest.approx(8.0)


def test_stats_cli_filter_since(isolated_home: Path) -> None:
    missions_dir = isolated_home / "projects" / "proj1" / "missions"
    _write_meta(missions_dir, "m-old", started_at="2026-01-01T00:00:00Z")
    _write_meta(missions_dir, "m-new", started_at="2026-05-10T00:00:00Z")

    runner = CliRunner()
    result = runner.invoke(app, ["stats", "--since", "2026-05-01", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output.strip())
    assert data["total_missions"] == 1


def test_stats_cli_filter_specialist(isolated_home: Path) -> None:
    missions_dir = isolated_home / "projects" / "proj1" / "missions"
    _write_meta(missions_dir, "m-0001", specialist="aria")
    _write_meta(missions_dir, "m-0002", specialist="ben")

    runner = CliRunner()
    result = runner.invoke(app, ["stats", "--specialist", "ben", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output.strip())
    assert data["total_missions"] == 1
    assert data["by_specialist"][0]["specialist"] == "ben"
