"""Tests for workforce.budget."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from workforce.budget import BudgetCheckResult, _sum_monthly_cost, check_budget
from workforce.project import Project


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(
    *,
    monthly_limit_usd: float | None = None,
    per_mission_limit_usd: float | None = None,
    alert_threshold_pct: int = 80,
) -> Project:
    return Project(
        id="aabbccddeeff",
        name="test-project",
        repo_path="/tmp/repo",
        monthly_limit_usd=monthly_limit_usd,
        per_mission_limit_usd=per_mission_limit_usd,
        alert_threshold_pct=alert_threshold_pct,
    )


def _write_meta(
    missions_dir: Path,
    mission_id: str,
    *,
    status: str = "completed",
    cost_usd: float = 1.0,
    started_at: str | None = None,
) -> None:
    """Write a synthetic meta.json for one mission."""
    if started_at is None:
        now = dt.datetime.now(dt.UTC)
        started_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    mission_dir = missions_dir / mission_id
    mission_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema_version": 1,
        "mission_id": mission_id,
        "project_id": "aabbccddeeff",
        "project_name": "test-project",
        "specialist": "builder",
        "model": "claude-sonnet-4-6",
        "ticket": "do something",
        "started_at": started_at,
        "ended_at": started_at,
        "duration_seconds": 60.0,
        "status": status,
        "cost_usd": cost_usd,
        "manager_cost_usd": 0.0,
        "review_cost_usd": 0.0,
        "turn_count": 10,
        "commits": [],
        "memory_delta_captured": False,
        "reviews": [],
        "revision_rounds": 0,
    }
    (mission_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _write_parallel_meta(missions_dir: Path, mission_id: str, *, cost_usd: float = 5.0) -> None:
    """Write a synthetic parallel parent meta.json (has parent_mission_id key)."""
    mission_dir = missions_dir / mission_id
    mission_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {
        "parent_mission_id": mission_id,
        "project_id": "aabbccddeeff",
        "status": "completed",
        "cost_usd": cost_usd,
        "started_at": now,
        "sub_missions": [],
    }
    (mission_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


# ---------------------------------------------------------------------------
# _sum_monthly_cost
# ---------------------------------------------------------------------------


class TestSumMonthlyCost:
    def test_no_missions_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        assert _sum_monthly_cost("aabbccddeeff") == 0.0

    def test_empty_missions_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        assert _sum_monthly_cost("aabbccddeeff") == 0.0

    def test_counts_completed_missions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", status="completed", cost_usd=1.0)
        _write_meta(missions, "m-002", status="completed", cost_usd=0.5)
        total = _sum_monthly_cost("aabbccddeeff")
        assert abs(total - 1.5) < 1e-9

    def test_counts_error_missions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", status="error", cost_usd=0.25)
        total = _sum_monthly_cost("aabbccddeeff")
        assert abs(total - 0.25) < 1e-9

    def test_skips_non_completed_statuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", status="wall_timeout", cost_usd=2.0)
        _write_meta(missions, "m-002", status="interrupted", cost_usd=3.0)
        assert _sum_monthly_cost("aabbccddeeff") == 0.0

    def test_skips_parallel_parent_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        # Parallel parent meta aggregates child costs — must not be double-counted.
        _write_parallel_meta(missions, "m-parent", cost_usd=10.0)
        _write_meta(missions, "m-child", status="completed", cost_usd=2.0)
        total = _sum_monthly_cost("aabbccddeeff")
        assert abs(total - 2.0) < 1e-9

    def test_skips_previous_month_missions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        # Current month mission
        _write_meta(missions, "m-current", status="completed", cost_usd=1.0)
        # Previous month mission
        last_month = dt.datetime.now(dt.UTC).replace(day=1) - dt.timedelta(days=1)
        old_ts = last_month.strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_meta(missions, "m-old", status="completed", cost_usd=99.0, started_at=old_ts)
        total = _sum_monthly_cost("aabbccddeeff")
        assert abs(total - 1.0) < 1e-9

    def test_skips_corrupted_meta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        bad_dir = missions / "m-bad"
        bad_dir.mkdir()
        (bad_dir / "meta.json").write_text("not json")
        _write_meta(missions, "m-good", status="completed", cost_usd=0.5)
        assert abs(_sum_monthly_cost("aabbccddeeff") - 0.5) < 1e-9

    def test_skips_missing_started_at(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        bad_dir = missions / "m-nodate"
        bad_dir.mkdir()
        (bad_dir / "meta.json").write_text(
            json.dumps({"status": "completed", "cost_usd": 5.0})
        )
        assert _sum_monthly_cost("aabbccddeeff") == 0.0


# ---------------------------------------------------------------------------
# check_budget
# ---------------------------------------------------------------------------


class TestCheckBudget:
    def test_no_limit_always_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        proj = _make_project()  # monthly_limit_usd=None
        result = check_budget(proj.id, proj)
        assert result == BudgetCheckResult(allowed=True, warning=None, reason=None)

    def test_zero_limit_always_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Treat 0 the same as "no limit" to avoid accidental lockout.
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        proj = _make_project(monthly_limit_usd=0.0)
        result = check_budget(proj.id, proj)
        assert result.allowed is True

    def test_below_threshold_allowed_no_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", cost_usd=1.0)  # $1 of $5 = 20%
        proj = _make_project(monthly_limit_usd=5.0, alert_threshold_pct=80)
        result = check_budget(proj.id, proj)
        assert result == BudgetCheckResult(allowed=True, warning=None, reason=None)

    def test_at_threshold_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", cost_usd=4.0)  # $4 of $5 = 80%
        proj = _make_project(monthly_limit_usd=5.0, alert_threshold_pct=80)
        result = check_budget(proj.id, proj)
        assert result.allowed is True
        assert result.warning is not None
        assert "80%" in result.warning
        assert result.reason is None

    def test_above_threshold_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", cost_usd=4.5)  # $4.50 of $5 = 90%
        proj = _make_project(monthly_limit_usd=5.0, alert_threshold_pct=80)
        result = check_budget(proj.id, proj)
        assert result.allowed is True
        assert result.warning is not None
        assert result.reason is None

    def test_at_limit_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", cost_usd=5.0)  # exactly at $5 limit
        proj = _make_project(monthly_limit_usd=5.0)
        result = check_budget(proj.id, proj)
        assert result.allowed is False
        assert result.reason is not None
        assert "exhausted" in result.reason
        assert result.warning is None

    def test_over_limit_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", cost_usd=6.0)
        proj = _make_project(monthly_limit_usd=5.0)
        result = check_budget(proj.id, proj)
        assert result.allowed is False

    def test_new_cost_estimate_pushes_over_limit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", cost_usd=4.5)  # $4.50 spent
        proj = _make_project(monthly_limit_usd=5.0)
        # Without estimate: $4.50 < $5, allowed.
        assert check_budget(proj.id, proj).allowed is True
        # With $1 estimate: $5.50 >= $5, blocked.
        result = check_budget(proj.id, proj, new_cost_estimate=1.0)
        assert result.allowed is False

    def test_new_cost_estimate_pushes_over_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", cost_usd=3.0)  # $3 of $5 = 60%
        proj = _make_project(monthly_limit_usd=5.0, alert_threshold_pct=80)
        # Without estimate: 60% < 80%, no warning.
        assert check_budget(proj.id, proj).warning is None
        # With $1.5 estimate: $4.50 of $5 = 90%, warning.
        result = check_budget(proj.id, proj, new_cost_estimate=1.5)
        assert result.allowed is True
        assert result.warning is not None

    def test_empty_project_no_missions_allowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        proj = _make_project(monthly_limit_usd=5.0)
        result = check_budget(proj.id, proj)
        assert result == BudgetCheckResult(allowed=True, warning=None, reason=None)

    def test_warning_contains_dollar_amounts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", cost_usd=4.1)
        proj = _make_project(monthly_limit_usd=5.0, alert_threshold_pct=80)
        result = check_budget(proj.id, proj)
        assert result.warning is not None
        assert "$4.10" in result.warning
        assert "$5.00" in result.warning

    def test_reason_contains_dollar_amounts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
        missions = tmp_path / "projects" / "aabbccddeeff" / "missions"
        missions.mkdir(parents=True)
        _write_meta(missions, "m-001", cost_usd=5.5)
        proj = _make_project(monthly_limit_usd=5.0)
        result = check_budget(proj.id, proj)
        assert result.reason is not None
        assert "$5.50" in result.reason
        assert "$5.00" in result.reason
