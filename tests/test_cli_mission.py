from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from workforce.cli import app
from workforce.cli._common import _summarize_tool_args, _truncate
from workforce.cli.cleanup import _parse_duration, _parse_iso_z
from workforce.mission import MissionMeta, MissionStatus, mission_paths
from workforce.parallel import ParallelMissionMeta, ParallelStatus, SubMissionRef
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


def _result_event(num_turns: int = 3, cost: float = 0.01) -> str:
    """One-line JSON ResultMessage event."""
    return json.dumps({
        "_type": "ResultMessage",
        "num_turns": num_turns,
        "total_cost_usd": cost,
        "duration_ms": 1234,
    }) + "\n"


def test_tail_auto_terminates_on_result_message(
    tail_mission: tuple[Project, str],
) -> None:
    """follow=True exits automatically once a ResultMessage event is seen.

    The implementation sleeps one extra poll cycle after seeing ResultMessage
    (to catch trailing events), then returns.  We mock time.sleep so the test
    runs instantly: the first sleep call (normal poll) triggers the file write
    with a ResultMessage; the second sleep call is the trailing-events sleep
    that happens right before return — we let it pass so the function exits.
    """
    proj, mid = tail_mission
    mp = mission_paths(proj.id, mid)
    mp.events.write_text(_assistant_event("line before result"))

    sleep_call = 0

    def fake_sleep(seconds: float) -> None:
        nonlocal sleep_call
        sleep_call += 1
        if sleep_call == 1:
            # After first poll, append a ResultMessage so the next iteration
            # sees it and sets result_seen = True.
            with mp.events.open("a") as fh:
                fh.write(_result_event())
        # sleep_call >= 2: the trailing-events sleep before return — do nothing.

    with patch("time.sleep", side_effect=fake_sleep):
        runner = CliRunner()
        result = runner.invoke(app, ["mission", "tail", mid, "--poll", "0.01"])

    assert result.exit_code == 0, result.output
    assert "line before result" in result.output
    # The function should print "(mission ended)" before returning.
    assert "mission ended" in result.output
    # It should NOT print "(stopped)" — that's the KeyboardInterrupt path.
    assert "stopped" not in result.output


def test_tail_timeout_exits_with_error_when_mission_does_not_finish(
    tail_mission: tuple[Project, str],
) -> None:
    """--timeout N exits with a non-zero exit code if no ResultMessage in time."""
    proj, mid = tail_mission
    mp = mission_paths(proj.id, mid)
    mp.events.write_text(_assistant_event("running…"))

    # Use a real tiny timeout so the test completes quickly.
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["mission", "tail", mid, "--poll", "0.05", "--timeout", "0.1"],
    )

    # Should exit non-zero (die() raises SystemExit(1)).
    assert result.exit_code != 0
    assert "timeout" in result.output.lower()


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_single_meta(
    proj: Project,
    spec: Specialist,
    mid: str,
    *,
    worktree_path: str | None = None,
    base_sha: str | None = None,
) -> MissionMeta:
    """Create a MissionMeta and write it (+ ticket.md) to disk."""
    mp = mission_paths(proj.id, mid)
    mp.root.mkdir(parents=True, exist_ok=True)
    meta = MissionMeta(
        mission_id=mid,
        project_id=proj.id,
        project_name=proj.name,
        specialist=spec.name,
        model=spec.model,
        ticket="the original ticket text",
        branch=None if base_sha is None else f"workforce/{mid}",
        worktree_path=worktree_path,
        base_sha=base_sha,
        started_at="2026-05-03T00:00:00Z",
        ended_at="2026-05-03T00:01:00Z",
        duration_seconds=60.0,
        status=MissionStatus.COMPLETED,
    )
    mp.meta.write_text(meta.model_dump_json(indent=2) + "\n")
    mp.ticket.write_text("the original ticket text")
    return meta


# ----- mission retry ---------------------------------------------------------


def test_retry_dispatches_same_specialist(
    workspace_setup: tuple[Project, Specialist],
) -> None:
    """retry calls _dispatch_direct with the original ticket and same specialist."""
    proj, spec = workspace_setup
    mid = "m-retry-001"
    _make_single_meta(proj, spec, mid)

    calls: list[dict[str, Any]] = []

    def fake_dispatch_direct(
        p: Any, ticket: str, sp: Any, *args: Any, **kwargs: Any
    ) -> None:
        calls.append({"ticket": ticket, "specialist": sp.name})

    runner = CliRunner()
    with patch("workforce.cli.dispatch._dispatch_direct", side_effect=fake_dispatch_direct):
        result = runner.invoke(app, ["mission", "retry", mid])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["ticket"] == "the original ticket text"
    assert calls[0]["specialist"] == spec.name


def test_retry_prints_new_mission_id(
    workspace_setup: tuple[Project, Specialist],
) -> None:
    """retry prints the new mission id before starting the dispatch."""
    proj, spec = workspace_setup
    mid = "m-retry-002"
    _make_single_meta(proj, spec, mid)

    runner = CliRunner()
    with patch("workforce.cli.dispatch._dispatch_direct"):
        result = runner.invoke(app, ["mission", "retry", mid])

    assert result.exit_code == 0, result.output
    # A new mission id (starts with 'm-') should appear in the output.
    assert any(token.startswith("m-") for token in result.output.split())


def test_retry_background_calls_dispatch_detached(
    workspace_setup: tuple[Project, Specialist],
) -> None:
    """retry --background delegates to _dispatch_detached (no terminal window)."""
    proj, spec = workspace_setup
    mid = "m-retry-003"
    _make_single_meta(proj, spec, mid)

    detached_calls: list[dict[str, Any]] = []

    def fake_detached(**kwargs: Any) -> None:
        detached_calls.append(kwargs)
        # _dispatch_detached normally calls output.success; simulate it.
        from workforce import output as _out
        _out.success("dispatched mission m-fake-001")

    runner = CliRunner()
    with patch("workforce.cli.dispatch._dispatch_detached", side_effect=fake_detached):
        result = runner.invoke(app, ["mission", "retry", mid, "--background"])

    assert result.exit_code == 0, result.output
    assert len(detached_calls) == 1
    assert detached_calls[0]["specialist"] == spec.name
    assert detached_calls[0]["ticket"] == "the original ticket text"
    assert detached_calls[0]["open_window"] is False


def test_retry_dies_when_ticket_md_missing(
    workspace_setup: tuple[Project, Specialist],
) -> None:
    """retry should die with a clear message if ticket.md is absent."""
    proj, spec = workspace_setup
    mid = "m-retry-004"
    mp = mission_paths(proj.id, mid)
    mp.root.mkdir(parents=True, exist_ok=True)
    # Write meta but NOT ticket.md.
    meta = MissionMeta(
        mission_id=mid,
        project_id=proj.id,
        project_name=proj.name,
        specialist=spec.name,
        model=spec.model,
        ticket="t",
        branch=None,
        worktree_path=None,
        base_sha=None,
        started_at="2026-05-03T00:00:00Z",
        ended_at="2026-05-03T00:01:00Z",
        duration_seconds=60.0,
        status=MissionStatus.COMPLETED,
    )
    mp.meta.write_text(meta.model_dump_json(indent=2) + "\n")

    runner = CliRunner()
    result = runner.invoke(app, ["mission", "retry", mid])

    assert result.exit_code != 0
    assert "ticket.md" in result.output


def test_retry_parallel_background_is_rejected(
    workspace_setup: tuple[Project, Specialist],
) -> None:
    """retry --background on a parallel parent mission should die with an error."""
    proj, spec = workspace_setup
    parent_mid = "m-retry-par-001"
    mp = mission_paths(proj.id, parent_mid)
    mp.root.mkdir(parents=True, exist_ok=True)
    from workforce.manager import DecompositionKind
    parent_meta = ParallelMissionMeta(
        parent_mission_id=parent_mid,
        project_id=proj.id,
        project_name=proj.name,
        ticket="parallel ticket",
        started_at="2026-05-03T00:00:00Z",
        manager_cost_usd=0.01,
        sub_missions=[],
        status=ParallelStatus.COMPLETED,
        decomposition_kind=DecompositionKind.PARALLEL,
    )
    mp.meta.write_text(parent_meta.model_dump_json(indent=2) + "\n")
    mp.ticket.write_text("parallel ticket")

    runner = CliRunner()
    result = runner.invoke(app, ["mission", "retry", parent_mid, "--background"])

    assert result.exit_code != 0
    assert "background" in result.output.lower()


# ----- mission diff ----------------------------------------------------------


def test_diff_calls_git_diff(
    workspace_setup: tuple[Project, Specialist], tmp_path: Path
) -> None:
    """diff runs 'git diff <sha>..HEAD' in the worktree directory."""
    proj, spec = workspace_setup
    mid = "m-diff-001"
    fake_worktree = tmp_path / "worktree"
    fake_worktree.mkdir()
    _make_single_meta(proj, spec, mid, worktree_path=str(fake_worktree), base_sha="abc1234")

    calls: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append({"cmd": cmd, "cwd": kwargs.get("cwd")})
        return MagicMock(returncode=0)

    runner = CliRunner()
    with patch("subprocess.run", side_effect=fake_run):
        result = runner.invoke(app, ["mission", "diff", mid])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["cmd"] == ["git", "diff", "abc1234..HEAD"]
    assert calls[0]["cwd"] == fake_worktree


def test_diff_stat_flag(
    workspace_setup: tuple[Project, Specialist], tmp_path: Path
) -> None:
    """diff --stat inserts --stat into the git command."""
    proj, spec = workspace_setup
    mid = "m-diff-002"
    fake_worktree = tmp_path / "worktree2"
    fake_worktree.mkdir()
    _make_single_meta(proj, spec, mid, worktree_path=str(fake_worktree), base_sha="deadbeef")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append(cmd)
        return MagicMock(returncode=0)

    runner = CliRunner()
    with patch("subprocess.run", side_effect=fake_run):
        result = runner.invoke(app, ["mission", "diff", mid, "--stat"])

    assert result.exit_code == 0, result.output
    assert calls[0] == ["git", "diff", "--stat", "deadbeef..HEAD"]


def test_diff_workspace_mission_warns_no_crash(
    workspace_setup: tuple[Project, Specialist],
) -> None:
    """diff on a workspace mission (base_sha=None) warns and exits cleanly."""
    proj, spec = workspace_setup
    mid = "m-diff-003"
    _make_single_meta(proj, spec, mid)  # no worktree_path or base_sha

    runner = CliRunner()
    result = runner.invoke(app, ["mission", "diff", mid])

    assert result.exit_code == 0, result.output
    assert "no base_sha" in result.output or "workspace" in result.output


def test_diff_missing_worktree_warns(
    workspace_setup: tuple[Project, Specialist], tmp_path: Path
) -> None:
    """diff warns gracefully when the worktree directory no longer exists."""
    proj, spec = workspace_setup
    mid = "m-diff-004"
    gone = tmp_path / "gone"
    # Intentionally do NOT create the directory.
    _make_single_meta(proj, spec, mid, worktree_path=str(gone), base_sha="cafebabe")

    runner = CliRunner()
    result = runner.invoke(app, ["mission", "diff", mid])

    assert result.exit_code == 0, result.output
    assert "no longer exists" in result.output


def test_diff_parallel_mission_iterates_sub_missions(
    workspace_setup: tuple[Project, Specialist], tmp_path: Path
) -> None:
    """diff on a parallel parent iterates sub-missions and diffs each one."""
    proj, spec = workspace_setup
    parent_mid = "m-diff-par-001"
    sub1_mid = f"{parent_mid}__task-a"
    sub2_mid = f"{parent_mid}__task-b"

    # Write parent meta.
    pmp = mission_paths(proj.id, parent_mid)
    pmp.root.mkdir(parents=True, exist_ok=True)
    from workforce.manager import DecompositionKind
    parent_meta = ParallelMissionMeta(
        parent_mission_id=parent_mid,
        project_id=proj.id,
        project_name=proj.name,
        ticket="parallel ticket",
        started_at="2026-05-03T00:00:00Z",
        manager_cost_usd=0.01,
        sub_missions=[
            SubMissionRef(task_id="task-a", mission_id=sub1_mid, specialist=spec.name),
            SubMissionRef(task_id="task-b", mission_id=sub2_mid, specialist=spec.name),
        ],
        status=ParallelStatus.COMPLETED,
        decomposition_kind=DecompositionKind.PARALLEL,
    )
    pmp.meta.write_text(parent_meta.model_dump_json(indent=2) + "\n")

    # Write sub-mission metas with real worktrees.
    wt1 = tmp_path / "wt1"
    wt1.mkdir()
    wt2 = tmp_path / "wt2"
    wt2.mkdir()
    _make_single_meta(proj, spec, sub1_mid, worktree_path=str(wt1), base_sha="sha1111")
    _make_single_meta(proj, spec, sub2_mid, worktree_path=str(wt2), base_sha="sha2222")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        calls.append(cmd)
        return MagicMock(returncode=0)

    runner = CliRunner()
    with patch("subprocess.run", side_effect=fake_run):
        result = runner.invoke(app, ["mission", "diff", parent_mid])

    assert result.exit_code == 0, result.output
    assert len(calls) == 2
    # Both sub-missions' base SHAs appear in the git commands.
    cmds_str = str(calls)
    assert "sha1111" in cmds_str
    assert "sha2222" in cmds_str
