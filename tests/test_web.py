"""Tests for the workforce web dashboard (src/workforce/web/).

Requires workforce[dev] which includes fastapi and httpx.
All filesystem I/O is mocked so no real WORKFORCE_HOME is needed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Skip the whole module if FastAPI / httpx are not installed.
fastapi = pytest.importorskip("fastapi", reason="fastapi not installed")
pytest.importorskip("httpx", reason="httpx not installed (needed for TestClient)")

from fastapi.testclient import TestClient  # noqa: E402

from workforce.mission import MissionMeta, MissionPaths, MissionStatus  # noqa: E402
from workforce.specialist import Specialist, SpecialistStats  # noqa: E402
from workforce.web.app import app  # noqa: E402


# ── fixtures & helpers ───────────────────────────────────────────────────────


def _make_meta(**kwargs: object) -> MissionMeta:
    defaults: dict[str, object] = dict(
        mission_id="m-20260501-120000-abcd",
        project_id="abc123def456",
        project_name="test-project",
        specialist="builder",
        model="claude-sonnet-4-6",
        ticket="Do something",
        started_at="2026-05-01T12:00:00Z",
        ended_at="2026-05-01T12:05:00Z",
        duration_seconds=300.0,
        status=MissionStatus.COMPLETED,
        cost_usd=0.05,
    )
    defaults.update(kwargs)
    return MissionMeta(**defaults)  # type: ignore[arg-type]


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def sample_meta() -> MissionMeta:
    return _make_meta()


@pytest.fixture()
def mission_files(tmp_path: Path) -> tuple[MissionMeta, MissionPaths]:
    meta = _make_meta(
        worktree_path=str(tmp_path),
        base_sha="deadbeef0000",
        branch="workforce/m-20260501-120000-abcd",
    )
    mp = MissionPaths(root=tmp_path)
    mp.ticket.write_text("Do something important\n")
    mp.result.write_text("I completed the task successfully.\n")
    return meta, mp


# ── GET / ────────────────────────────────────────────────────────────────────


def test_mission_list_empty(client: TestClient) -> None:
    with patch("workforce.web.app._load_all_missions", return_value=[]):
        resp = client.get("/")
    assert resp.status_code == 200
    assert "Missions" in resp.text


def test_mission_list_shows_mission(client: TestClient, sample_meta: MissionMeta) -> None:
    with patch("workforce.web.app._load_all_missions", return_value=[sample_meta]):
        resp = client.get("/")
    assert resp.status_code == 200
    assert sample_meta.mission_id in resp.text
    assert sample_meta.project_name in resp.text
    assert sample_meta.specialist in resp.text


def test_mission_list_filter_by_project(client: TestClient) -> None:
    alpha = _make_meta(project_name="alpha")
    beta = _make_meta(mission_id="m-20260501-120000-efgh", project_name="beta")
    with patch("workforce.web.app._load_all_missions", return_value=[alpha, beta]):
        resp = client.get("/?project=alpha")
    assert resp.status_code == 200
    # alpha's mission ID should appear in the table rows; beta's should not
    assert alpha.mission_id in resp.text
    assert beta.mission_id not in resp.text


def test_mission_list_filter_by_status(client: TestClient) -> None:
    done = _make_meta(status=MissionStatus.COMPLETED)
    failed = _make_meta(
        mission_id="m-20260501-120000-efgh",
        status=MissionStatus.ERROR,
    )
    with patch("workforce.web.app._load_all_missions", return_value=[done, failed]):
        resp = client.get("/?status=completed")
    assert resp.status_code == 200
    assert done.mission_id in resp.text
    assert failed.mission_id not in resp.text


def test_mission_list_filter_by_since(client: TestClient) -> None:
    old = _make_meta(started_at="2026-04-01T00:00:00Z")
    new = _make_meta(
        mission_id="m-20260501-120000-efgh",
        started_at="2026-05-10T00:00:00Z",
    )
    with patch("workforce.web.app._load_all_missions", return_value=[old, new]):
        resp = client.get("/?since=2026-05-01")
    assert resp.status_code == 200
    assert new.mission_id in resp.text
    assert old.mission_id not in resp.text


# ── GET /mission/{id} ────────────────────────────────────────────────────────


def test_mission_detail_not_found(client: TestClient) -> None:
    with patch("workforce.web.app._load_all_missions", return_value=[]):
        resp = client.get("/mission/m-does-not-exist")
    assert resp.status_code == 404


def test_mission_detail(
    client: TestClient,
    mission_files: tuple[MissionMeta, MissionPaths],
) -> None:
    meta, mp = mission_files
    with (
        patch("workforce.web.app._load_all_missions", return_value=[meta]),
        patch("workforce.web.app.mission_paths", return_value=mp),
    ):
        resp = client.get(f"/mission/{meta.mission_id}")
    assert resp.status_code == 200
    assert "Do something important" in resp.text
    assert "I completed the task successfully." in resp.text
    assert meta.mission_id in resp.text
    # Links to diff and tail button should be present
    assert "/diff" in resp.text
    assert "startTail" in resp.text


def test_mission_detail_missing_files(client: TestClient, tmp_path: Path) -> None:
    meta = _make_meta()
    mp = MissionPaths(root=tmp_path)
    # No ticket.md or result.md created — should fall back gracefully
    with (
        patch("workforce.web.app._load_all_missions", return_value=[meta]),
        patch("workforce.web.app.mission_paths", return_value=mp),
    ):
        resp = client.get(f"/mission/{meta.mission_id}")
    assert resp.status_code == 200
    assert "(no ticket)" in resp.text
    assert "(no result)" in resp.text


# ── GET /mission/{id}/diff ───────────────────────────────────────────────────


def test_mission_diff_no_worktree(client: TestClient) -> None:
    meta = _make_meta(worktree_path=None, base_sha=None)
    with patch("workforce.web.app._load_all_missions", return_value=[meta]):
        resp = client.get(f"/mission/{meta.mission_id}/diff")
    assert resp.status_code == 200
    assert "No worktree" in resp.text or "workspace" in resp.text.lower()


def test_mission_diff_not_found(client: TestClient) -> None:
    with patch("workforce.web.app._load_all_missions", return_value=[]):
        resp = client.get("/mission/m-no-such/diff")
    assert resp.status_code == 404


def test_mission_diff_with_output(client: TestClient, tmp_path: Path) -> None:
    meta = _make_meta(worktree_path=str(tmp_path), base_sha="deadbeef")
    fake_diff = "--- a/foo.py\n+++ b/foo.py\n-old line\n+new line\n context\n"
    mock_result = MagicMock(spec=subprocess.CompletedProcess)
    mock_result.stdout = fake_diff
    mock_result.returncode = 0
    with (
        patch("workforce.web.app._load_all_missions", return_value=[meta]),
        patch("workforce.web.app.subprocess.run", return_value=mock_result),
    ):
        resp = client.get(f"/mission/{meta.mission_id}/diff")
    assert resp.status_code == 200
    assert "diff-add" in resp.text
    assert "+new line" in resp.text
    assert "diff-del" in resp.text
    assert "-old line" in resp.text


def test_mission_diff_missing_worktree_dir(client: TestClient) -> None:
    meta = _make_meta(worktree_path="/nonexistent/path/xyz", base_sha="abc123")
    with patch("workforce.web.app._load_all_missions", return_value=[meta]):
        resp = client.get(f"/mission/{meta.mission_id}/diff")
    assert resp.status_code == 200
    assert "worktree path not found" in resp.text or "not found" in resp.text.lower()


# ── GET /stats ───────────────────────────────────────────────────────────────


def test_stats_empty(client: TestClient) -> None:
    with patch("workforce.web.app._load_all_missions", return_value=[]):
        resp = client.get("/stats")
    assert resp.status_code == 200
    assert "Stats" in resp.text
    assert "0" in resp.text  # total missions


def test_stats_shows_specialist(client: TestClient, sample_meta: MissionMeta) -> None:
    with patch("workforce.web.app._load_all_missions", return_value=[sample_meta]):
        resp = client.get("/stats")
    assert resp.status_code == 200
    assert sample_meta.specialist in resp.text
    assert "$0.0500" in resp.text  # formatted cost


def test_stats_success_rate(client: TestClient) -> None:
    done = _make_meta(status=MissionStatus.COMPLETED, cost_usd=0.1)
    fail = _make_meta(
        mission_id="m-20260501-120000-efgh",
        status=MissionStatus.ERROR,
        cost_usd=0.05,
    )
    with patch("workforce.web.app._load_all_missions", return_value=[done, fail]):
        resp = client.get("/stats")
    assert resp.status_code == 200
    assert "50%" in resp.text  # 1/2 completed


# ── GET /roster ──────────────────────────────────────────────────────────────


def _make_mock_rstore(specs: list[Specialist], stats: SpecialistStats | None = None) -> MagicMock:
    mock = MagicMock()
    mock.list.return_value = specs
    mock.load_stats.return_value = stats or SpecialistStats()
    mock.root = Path("/fake/roster")
    return mock


def test_roster_empty(client: TestClient) -> None:
    mock_rstore = _make_mock_rstore([])
    with (
        patch("workforce.web.app._load_all_missions", return_value=[]),
        patch("workforce.web.app.RosterStore", return_value=mock_rstore),
    ):
        resp = client.get("/roster")
    assert resp.status_code == 200
    assert "No specialists" in resp.text or "Roster" in resp.text


def test_roster_shows_specialist(client: TestClient) -> None:
    spec = Specialist(
        name="builder",
        role="Backend engineer",
        base_prompt="You are a builder.",
    )
    stats = SpecialistStats(missions_completed=3, missions_failed=1, total_cost_usd=0.42)
    mock_rstore = _make_mock_rstore([spec], stats)

    with (
        patch("workforce.web.app._load_all_missions", return_value=[]),
        patch("workforce.web.app.RosterStore", return_value=mock_rstore),
    ):
        resp = client.get("/roster")
    assert resp.status_code == 200
    assert "builder" in resp.text
    assert "Backend engineer" in resp.text
