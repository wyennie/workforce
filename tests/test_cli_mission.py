from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from workforce.cli import app
from workforce.cli._common import _summarize_tool_args, _truncate
from workforce.cli.cleanup import _parse_duration, _parse_iso_z
from workforce.mission import MissionMeta, MissionStatus, mission_paths
from workforce.project import Project, ProjectStore
from workforce.specialist import RosterStore, Specialist


@pytest.mark.parametrize(
    "s,expected",
    [
        ("7d", dt.timedelta(days=7)),
        ("24h", dt.timedelta(hours=24)),
        ("2w", dt.timedelta(weeks=2)),
        ("1m", dt.timedelta(days=30)),
        (" 30D ", dt.timedelta(days=30)),
    ],
)
def test_parse_duration_valid(s: str, expected: dt.timedelta) -> None:
    assert _parse_duration(s) == expected


@pytest.mark.parametrize("bad", ["", "7", "7y", "abc", "7 days", "-7d"])
def test_parse_duration_invalid_raises(bad: str) -> None:
    with pytest.raises(typer.BadParameter):
        _parse_duration(bad)


def test_parse_iso_z() -> None:
    parsed = _parse_iso_z("2026-05-02T14:12:34Z")
    assert parsed == dt.datetime(2026, 5, 2, 14, 12, 34, tzinfo=dt.UTC)


def test_truncate_short() -> None:
    assert _truncate("hello", 10) == "hello"


def test_truncate_long() -> None:
    out = _truncate("hello world this is too long", 10)
    assert len(out) == 10
    assert out.endswith("…")


def test_summarize_tool_args_picks_known_key() -> None:
    assert "file_path=" in _summarize_tool_args("Write", {"file_path": "/tmp/x", "content": "..."})
    assert "command=" in _summarize_tool_args("Bash", {"command": "ls -la"})


def test_summarize_tool_args_falls_back_to_first() -> None:
    out = _summarize_tool_args("Custom", {"some_arg": 42})
    assert "some_arg=" in out


def test_summarize_tool_args_empty() -> None:
    assert _summarize_tool_args("X", {}) == ""


# ----- workspace dispatch gates ---------------------------------------------

# These tests exercise the early-return paths in dispatch_command for workspace
# projects. They never get far enough to call the runner or Manager, so no
# heavyweight mocks are needed — just an isolated WORKFORCE_HOME, a workspace
# project on disk, and one assigned specialist.


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WORKFORCE_HOME", str(home))
    return home


@pytest.fixture
def workspace_setup(
    isolated_home: Path, tmp_path: Path
) -> tuple[Project, Specialist]:
    """Register a workspace project with one assigned specialist."""
    isolated_home.mkdir(parents=True, exist_ok=True)
    ws = tmp_path / "ws"
    ws.mkdir()
    rs = RosterStore()
    spec = Specialist.from_template("aria", "backend")
    rs.save(spec)
    ps = ProjectStore()
    proj = Project(
        id="def456abc789",
        name="myws",
        repo_path=str(ws),
        kind="workspace",
        assigned_specialists=[spec.name],
    )
    ps.save(proj)
    return proj, spec


def test_dispatch_workspace_rejects_auto_merge(
    workspace_setup: tuple[Project, Specialist],
) -> None:
    _, spec = workspace_setup
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["dispatch", "myws", "do a thing", "--specialist", spec.name, "--auto-merge"],
    )
    assert result.exit_code != 0
    combined = result.output or ""
    assert "auto-merge" in combined.lower() or "merge-into" in combined.lower()


def test_dispatch_workspace_rejects_review(
    workspace_setup: tuple[Project, Specialist],
) -> None:
    _, spec = workspace_setup
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["dispatch", "myws", "do a thing", "--specialist", spec.name, "--review"],
    )
    assert result.exit_code != 0
    combined = result.output or ""
    assert "review" in combined.lower()


def test_dispatch_window_requires_specialist(
    workspace_setup: tuple[Project, Specialist],
) -> None:
    """`--window` dispatches one mission and pops a terminal — it doesn't
    make sense without --specialist."""
    runner = CliRunner()
    result = runner.invoke(app, ["dispatch", "myws", "do a thing", "--window"])
    assert result.exit_code != 0
    flat = " ".join(result.output.split())
    assert "--window requires --specialist" in flat


def test_dispatch_background_requires_specialist(
    workspace_setup: tuple[Project, Specialist],
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["dispatch", "myws", "do a thing", "--background"])
    assert result.exit_code != 0
    flat = " ".join(result.output.split())
    assert "--background requires --specialist" in flat


def test_dispatch_window_and_background_mutually_exclusive(
    workspace_setup: tuple[Project, Specialist],
) -> None:
    _, spec = workspace_setup
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["dispatch", "myws", "t", "--specialist", spec.name, "--window", "--background"],
    )
    assert result.exit_code != 0
    flat = " ".join(result.output.split())
    assert "mutually exclusive" in flat


def test_dispatch_background_does_not_open_window(
    workspace_setup: tuple[Project, Specialist], tmp_path: Path,
) -> None:
    """`--background` forks the dispatch but never opens a terminal — the
    Manager session is responsible for the shared tail window."""
    from unittest.mock import patch

    proj, spec = workspace_setup

    spawned_subprocesses: list[list[str]] = []
    spawned_windows: list[dict[str, Any]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        spawned_subprocesses.append(argv)
        return type("Fake", (), {"pid": 1, "wait": lambda self: 0})()

    def fake_open_window(title: str, command: list[str], **kwargs: object) -> bool:
        spawned_windows.append({"title": title, "command": command})
        return True

    runner = CliRunner()
    with patch("workforce.cli.dispatch.subprocess.Popen", side_effect=fake_popen):
        with patch("workforce.terminal.open_terminal_window", side_effect=fake_open_window):
            result = runner.invoke(
                app,
                ["dispatch", "myws", "ticket", "--specialist", spec.name, "--background"],
            )

    assert result.exit_code == 0, result.output
    # Subprocess was spawned with the pinned mission_id
    assert len(spawned_subprocesses) == 1
    argv = spawned_subprocesses[0]
    assert "--mission-id" in argv
    pinned_id = argv[argv.index("--mission-id") + 1]
    assert pinned_id.startswith("m-")
    # No --background or --window on the inner subprocess (would loop)
    assert "--background" not in argv
    assert "--window" not in argv
    # CRUCIAL: no terminal window opened
    assert spawned_windows == []
    # User-visible output mentions the mission id
    assert pinned_id in result.output


def test_dispatch_window_spawns_subprocess_and_terminal(
    workspace_setup: tuple[Project, Specialist], tmp_path: Path,
) -> None:
    """`--window` should: pre-allocate a mission id, spawn a detached subprocess
    re-invoking dispatch with that id, and call open_terminal_window with the
    matching tail command. We mock both subprocess.Popen and the terminal
    spawner so nothing actually runs or pops up."""
    from unittest.mock import patch

    proj, spec = workspace_setup

    spawned_subprocesses: list[list[str]] = []
    spawned_windows: list[dict[str, Any]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        spawned_subprocesses.append(argv)
        # Return a dummy object — the caller doesn't wait on it.
        return type("Fake", (), {"pid": 1, "wait": lambda self: 0})()

    def fake_open_window(title: str, command: list[str], **kwargs: object) -> bool:
        spawned_windows.append({"title": title, "command": command})
        return True

    runner = CliRunner()
    with patch("workforce.cli.dispatch.subprocess.Popen", side_effect=fake_popen):
        with patch("workforce.terminal.open_terminal_window", side_effect=fake_open_window):
            result = runner.invoke(
                app,
                ["dispatch", "myws", "ticket text", "--specialist", spec.name, "--window"],
            )

    assert result.exit_code == 0, result.output
    # One subprocess spawned, with --mission-id pinning the parent's id
    assert len(spawned_subprocesses) == 1
    argv = spawned_subprocesses[0]
    assert "dispatch" in argv
    assert "--mission-id" in argv
    pinned_id = argv[argv.index("--mission-id") + 1]
    assert pinned_id.startswith("m-")  # mission-id prefix
    # Subprocess does NOT have --window (would loop forever)
    assert "--window" not in argv
    # Specialist forwarded
    assert "--specialist" in argv
    assert spec.name in argv

    # One window opened, tailing the same mission id
    assert len(spawned_windows) == 1
    win = spawned_windows[0]
    assert pinned_id in win["title"]
    cmd = win["command"]
    assert "tail" in cmd
    assert pinned_id in cmd

    # User-facing output mentions the mission id
    assert pinned_id in result.output


def test_mission_clean_workspace_is_noop(
    workspace_setup: tuple[Project, Specialist], tmp_path: Path,
) -> None:
    """A workspace mission has no worktree; clean should report and exit 0."""
    proj, spec = workspace_setup
    # Hand-craft a workspace mission's meta.json (branch=None signals workspace).
    mid = "m-test-clean-1"
    mp = mission_paths(proj.id, mid)
    mp.root.mkdir(parents=True, exist_ok=True)
    meta = MissionMeta(
        mission_id=mid,
        project_id=proj.id,
        project_name=proj.name,
        specialist=spec.name,
        model=spec.model,
        ticket="t",
        branch=None,
        worktree_path=str(tmp_path / "ws"),
        base_sha=None,
        started_at="2026-05-03T00:00:00Z",
        ended_at="2026-05-03T00:01:00Z",
        duration_seconds=60.0,
        status=MissionStatus.COMPLETED,
    )
    mp.meta.write_text(meta.model_dump_json(indent=2) + "\n")
    # Round-trip via the file (sanity).
    json.loads(mp.meta.read_text())

    runner = CliRunner()
    result = runner.invoke(app, ["mission", "clean", mid, "-y"])
    assert result.exit_code == 0, result.output
    combined = result.output or ""
    assert "workspace mission" in combined.lower() or "nothing to clean" in combined.lower()


# ----- mission tail: polling loop -------------------------------------------

# These tests exercise the seek-based position tracking and follow-mode
# termination inside mission_tail (cli/mission.py ~lines 395-420).
# They use a real tmp file that grows between simulated poll iterations to
# verify that the loop reads only new content on subsequent passes.


@pytest.fixture
def tail_mission(
    workspace_setup: tuple[Project, Specialist],
) -> tuple[Project, str]:
    """Create a bare mission directory (no events.jsonl yet) under the workspace project."""
    proj, _spec = workspace_setup
    mid = "m-tail-test-001"
    mp = mission_paths(proj.id, mid)
    mp.root.mkdir(parents=True, exist_ok=True)
    return proj, mid


def _assistant_event(text: str) -> str:
    """One-line JSON AssistantMessage event whose text content renders visibly."""
    return json.dumps({
        "_type": "AssistantMessage",
        "content": [{"text": text}],
    }) + "\n"


def test_tail_no_follow_reads_events_and_exits(
    tail_mission: tuple[Project, str],
) -> None:
    """--no-follow reads all events present in events.jsonl and returns."""
    proj, mid = tail_mission
    mp = mission_paths(proj.id, mid)
    mp.events.write_text(_assistant_event("hello from tail"))

    runner = CliRunner()
    result = runner.invoke(app, ["mission", "tail", mid, "--no-follow"])

    assert result.exit_code == 0, result.output
    assert "hello from tail" in result.output


def test_tail_no_follow_handles_missing_events_file(
    tail_mission: tuple[Project, str],
) -> None:
    """--no-follow does not crash when events.jsonl hasn't been written yet.

    The FileNotFoundError is silently swallowed on the first (and only) pass;
    follow=False causes an immediate return with exit_code=0.
    """
    proj, mid = tail_mission
    # events.jsonl is intentionally absent — only the mission dir exists.
    mp = mission_paths(proj.id, mid)
    assert not mp.events.exists()

    runner = CliRunner()
    result = runner.invoke(app, ["mission", "tail", mid, "--no-follow"])

    assert result.exit_code == 0, result.output


def test_tail_no_follow_ignores_malformed_json_lines(
    tail_mission: tuple[Project, str],
) -> None:
    """Lines that fail JSON-decode are silently skipped."""
    proj, mid = tail_mission
    mp = mission_paths(proj.id, mid)
    mp.events.write_text(
        "not valid json\n"
        + _assistant_event("good line")
        + "{broken\n"
    )

    runner = CliRunner()
    result = runner.invoke(app, ["mission", "tail", mid, "--no-follow"])

    assert result.exit_code == 0, result.output
    assert "good line" in result.output


def test_tail_seek_tracks_position_between_iterations(
    tail_mission: tuple[Project, str],
) -> None:
    """The polling loop advances the file position after each pass so a
    subsequent pass reads only newly appended content, not the full file.

    Strategy: mock time.sleep so the first call appends a second event to
    the file, and the second call raises KeyboardInterrupt to stop the loop.
    The second event should appear exactly once in the output — if the loop
    re-read from byte 0 it would appear twice (or the first event would
    appear twice).
    """
    proj, mid = tail_mission
    mp = mission_paths(proj.id, mid)
    mp.events.write_text(_assistant_event("first event"))

    sleep_call = 0

    def fake_sleep(seconds: float) -> None:
        nonlocal sleep_call
        sleep_call += 1
        if sleep_call == 1:
            # Append a second event after the first iteration has read the file.
            with mp.events.open("a") as fh:
                fh.write(_assistant_event("second event"))
        else:
            raise KeyboardInterrupt

    with patch("time.sleep", side_effect=fake_sleep):
        runner = CliRunner()
        result = runner.invoke(app, ["mission", "tail", mid, "--poll", "0.01"])

    assert result.exit_code == 0, result.output
    assert "first event" in result.output
    assert "second event" in result.output
    # Seek-based tracking: each event appears exactly once.
    assert result.output.count("first event") == 1, (
        "first event appeared more than once — loop may be re-reading from start"
    )
    assert result.output.count("second event") == 1, (
        "second event appeared more than once — loop may be re-reading from start"
    )


def test_tail_follow_mode_terminates_on_keyboard_interrupt(
    tail_mission: tuple[Project, str],
) -> None:
    """In follow mode a KeyboardInterrupt exits cleanly (exit_code=0, no traceback)."""
    proj, mid = tail_mission
    mp = mission_paths(proj.id, mid)
    mp.events.write_text(_assistant_event("line before interrupt"))

    with patch("time.sleep", side_effect=KeyboardInterrupt):
        runner = CliRunner()
        result = runner.invoke(app, ["mission", "tail", mid, "--poll", "0.01"])

    assert result.exit_code == 0, result.output
    assert "line before interrupt" in result.output
    # The handler prints "(stopped)" to signal clean exit.
    assert "stopped" in result.output
