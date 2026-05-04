"""Tests for the four workforce MCP tool functions.

These tests do not require the ``mcp`` package to be installed — they only
exercise the tool implementations in ``workforce.mcp.tools``.

Because tools.py uses lazy imports inside function bodies (to avoid pulling in
heavy workforce modules at import time), patches must target the *source*
modules, not ``workforce.mcp.tools`` itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# workforce_dispatch
# ---------------------------------------------------------------------------


def test_dispatch_success():
    """Returns parsed JSON on exit-code 0."""
    from workforce.mcp.tools import workforce_dispatch

    payload = {"mission_id": "abc123", "status": "completed", "branch": "workforce/abc123"}
    proc = CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")

    with patch("subprocess.run", return_value=proc) as mock_run:
        result = workforce_dispatch("my-project", "Fix the bug")

    assert result == payload
    args = mock_run.call_args[0][0]
    assert args[:4] == ["workforce", "dispatch", "my-project", "Fix the bug"]
    assert "--ci" in args


def test_dispatch_with_specialist_and_auto_merge():
    """Passes --specialist and --auto-merge flags when specified."""
    from workforce.mcp.tools import workforce_dispatch

    payload = {"mission_id": "xyz", "status": "completed", "branch": "workforce/xyz"}
    proc = CompletedProcess(args=[], returncode=0, stdout=json.dumps(payload), stderr="")

    with patch("subprocess.run", return_value=proc) as mock_run:
        result = workforce_dispatch("proj", "ticket", specialist="builder", auto_merge=True)

    assert result == payload
    cmd = mock_run.call_args[0][0]
    assert "--specialist" in cmd
    assert "builder" in cmd
    assert "--auto-merge" in cmd


def test_dispatch_failure_returns_error():
    """Returns {'error': stderr} when the subprocess exits non-zero."""
    from workforce.mcp.tools import workforce_dispatch

    proc = CompletedProcess(args=[], returncode=1, stdout="", stderr="project not found")

    with patch("subprocess.run", return_value=proc):
        result = workforce_dispatch("bad-project", "ticket")

    assert result == {"error": "project not found"}


# ---------------------------------------------------------------------------
# workforce_mission_status
# ---------------------------------------------------------------------------


def test_mission_status_found():
    """Returns parsed meta dict when mission exists under a project."""
    from workforce.mcp.tools import workforce_mission_status

    meta = {"mission_id": "m1", "status": "completed", "cost_usd": 0.05}

    proj = MagicMock()
    proj.id = "proj-001"

    fake_meta_path = MagicMock(spec=Path)
    fake_meta_path.exists.return_value = True
    fake_meta_path.read_text.return_value = json.dumps(meta)

    fake_paths = MagicMock()
    fake_paths.meta = fake_meta_path

    fake_store = MagicMock()
    fake_store.list.return_value = [proj]

    with (
        patch("workforce.project.ProjectStore", return_value=fake_store),
        patch("workforce.mission.mission_paths", return_value=fake_paths),
    ):
        result = workforce_mission_status("m1")

    assert result == meta


def test_mission_status_not_found():
    """Returns error dict when mission is not found in any project."""
    from workforce.mcp.tools import workforce_mission_status

    proj = MagicMock()
    proj.id = "proj-001"

    fake_meta_path = MagicMock(spec=Path)
    fake_meta_path.exists.return_value = False

    fake_paths = MagicMock()
    fake_paths.meta = fake_meta_path

    fake_store = MagicMock()
    fake_store.list.return_value = [proj]

    with (
        patch("workforce.project.ProjectStore", return_value=fake_store),
        patch("workforce.mission.mission_paths", return_value=fake_paths),
    ):
        result = workforce_mission_status("no-such-id")

    assert result == {"error": "mission no-such-id not found"}


# ---------------------------------------------------------------------------
# workforce_roster
# ---------------------------------------------------------------------------


def test_roster_returns_specialist_summaries():
    """Returns one dict per specialist with name, role, missions, cost_usd."""
    from workforce.mcp.tools import workforce_roster

    spec = MagicMock()
    spec.role = "Senior backend engineer"

    stats = MagicMock()
    stats.missions_completed = 10
    stats.missions_failed = 2
    stats.total_cost_usd = 1.23

    fake_store = MagicMock()
    fake_store.names.return_value = ["builder"]
    fake_store.load.return_value = spec
    fake_store.load_stats.return_value = stats

    with patch("workforce.specialist.RosterStore", return_value=fake_store):
        result = workforce_roster()

    assert result == [
        {
            "name": "builder",
            "role": "Senior backend engineer",
            "missions": 12,  # 10 completed + 2 failed
            "cost_usd": 1.23,
        }
    ]


def test_roster_empty():
    """Returns empty list when roster is empty."""
    from workforce.mcp.tools import workforce_roster

    fake_store = MagicMock()
    fake_store.names.return_value = []

    with patch("workforce.specialist.RosterStore", return_value=fake_store):
        result = workforce_roster()

    assert result == []


# ---------------------------------------------------------------------------
# workforce_mission_result
# ---------------------------------------------------------------------------


def test_mission_result_found():
    """Returns result.md text when the file exists."""
    from workforce.mcp.tools import workforce_mission_result

    proj = MagicMock()
    proj.id = "proj-001"

    result_text = "# Mission complete\n\nAll done."
    result_path = MagicMock(spec=Path)
    result_path.exists.return_value = True
    result_path.read_text.return_value = result_text

    fake_root = MagicMock()
    fake_root.__truediv__ = lambda self, name: result_path  # root / "result.md"

    fake_paths = MagicMock()
    fake_paths.root = fake_root

    fake_store = MagicMock()
    fake_store.list.return_value = [proj]

    with (
        patch("workforce.project.ProjectStore", return_value=fake_store),
        patch("workforce.mission.mission_paths", return_value=fake_paths),
    ):
        result = workforce_mission_result("m1")

    assert result == result_text


def test_mission_result_not_found():
    """Returns error string when result.md is not found."""
    from workforce.mcp.tools import workforce_mission_result

    proj = MagicMock()
    proj.id = "proj-001"

    result_path = MagicMock(spec=Path)
    result_path.exists.return_value = False

    fake_root = MagicMock()
    fake_root.__truediv__ = lambda self, name: result_path

    fake_paths = MagicMock()
    fake_paths.root = fake_root

    fake_store = MagicMock()
    fake_store.list.return_value = [proj]

    with (
        patch("workforce.project.ProjectStore", return_value=fake_store),
        patch("workforce.mission.mission_paths", return_value=fake_paths),
    ):
        result = workforce_mission_result("ghost-id")

    assert result == "No result found for mission ghost-id"
