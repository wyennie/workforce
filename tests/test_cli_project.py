"""Integration tests for `workforce project` CLI commands."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from workforce.cli import app
from workforce.project import Project, ProjectStore
from workforce.specialist import RosterStore, Specialist


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WORKFORCE_HOME", str(home))
    return home


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "README.md").write_text("# t\n")
    subprocess.run(["git", "add", "README.md"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


@pytest.fixture
def plain_dir(tmp_path: Path) -> Path:
    d = tmp_path / "plain"
    d.mkdir()
    return d


def test_add_git_repo_autodetects_repo_kind(isolated_home: Path, git_repo: Path) -> None:
    """No flag, .git present → kind=repo (the existing engineering default)."""
    runner = CliRunner()
    result = runner.invoke(app, ["project", "add", str(git_repo), "--name", "myapp"])
    assert result.exit_code == 0, result.output
    proj = ProjectStore().resolve("myapp")
    assert proj.kind == "repo"
    assert proj.repo_path == str(git_repo)


def test_add_plain_dir_autodetects_workspace(isolated_home: Path, plain_dir: Path) -> None:
    """No flag, no .git → kind=workspace. No more error, no flag needed."""
    runner = CliRunner()
    result = runner.invoke(app, ["project", "add", str(plain_dir), "--name", "myws"])
    assert result.exit_code == 0, result.output
    proj = ProjectStore().resolve("myws")
    assert proj.kind == "workspace"
    assert proj.repo_path == str(plain_dir)


def test_add_workspace_flag_overrides_on_git_repo(
    isolated_home: Path, git_repo: Path,
) -> None:
    """`--workspace` forces workspace kind even when .git exists — for users
    who want non-engineering missions in a repo dir."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["project", "add", str(git_repo), "--name", "ws_in_repo", "--workspace"],
    )
    assert result.exit_code == 0, result.output
    proj = ProjectStore().resolve("ws_in_repo")
    assert proj.kind == "workspace"


def test_add_repo_flag_on_plain_dir_fails(isolated_home: Path, plain_dir: Path) -> None:
    """`--repo` is the explicit opt-in; fails loudly without .git."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["project", "add", str(plain_dir), "--name", "x", "--repo"],
    )
    assert result.exit_code != 0
    flat = " ".join(result.output.split())
    assert "not a git repository" in flat


def test_add_workspace_and_repo_flags_mutually_exclusive(
    isolated_home: Path, git_repo: Path,
) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["project", "add", str(git_repo), "--name", "x", "--workspace", "--repo"],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


# ----- project tail (multi-mission stream) ----------------------------------


def test_project_tail_renders_labeled_events_from_existing_missions(
    isolated_home: Path, plain_dir: Path,
) -> None:
    """Plant two mission dirs with events.jsonl; `project tail` should attach
    to both, render their events with `[short-id/specialist]` labels, and
    exit on Ctrl-C. We force exit by stubbing time.sleep to raise after one
    iteration."""
    # Register a workspace project.
    rs = RosterStore()
    rs.save(Specialist.from_template("aria", "backend"))
    rs.save(Specialist.from_template("ben", "frontend"))
    ps = ProjectStore()
    proj = Project(
        id="abc123def456",
        name="myws",
        repo_path=str(plain_dir),
        kind="workspace",
        assigned_specialists=["aria", "ben"],
    )
    ps.save(proj)

    # Plant two mission dirs with one assistant text event each + a meta.json
    # so the tail's label includes the specialist name.
    missions_dir = ps.missions_dir(proj.id)
    missions_dir.mkdir(parents=True, exist_ok=True)
    for mid, spec_name, text in [
        ("m-20260504-090000-aaaa", "aria", "scout reporting in"),
        ("m-20260504-090000-bbbb", "ben", "ben here"),
    ]:
        d = missions_dir / mid
        d.mkdir()
        (d / "events.jsonl").write_text(json.dumps({
            "_type": "AssistantMessage",
            "content": [{"text": text}],
        }) + "\n")
        (d / "meta.json").write_text(json.dumps({
            "specialist": spec_name,
        }))

    # Make time.sleep raise KeyboardInterrupt so the tail loop exits after
    # processing both missions once.
    sleep_count = {"n": 0}
    real_sleep = time.sleep

    def stub_sleep(s: float) -> None:
        sleep_count["n"] += 1
        if sleep_count["n"] >= 1:
            raise KeyboardInterrupt
        real_sleep(s)

    runner = CliRunner()
    with patch("workforce.cli_project.time.sleep", side_effect=stub_sleep):
        result = runner.invoke(app, ["project", "tail", "myws"])

    # Exits cleanly via the KeyboardInterrupt handler.
    assert result.exit_code == 0, result.output
    # Both missions attached, with labels including specialist name.
    assert "aaaa/aria" in result.output
    assert "bbbb/ben" in result.output
    # Both events rendered.
    assert "scout reporting in" in result.output
    assert "ben here" in result.output


def test_project_tail_handles_missions_dir_without_meta(
    isolated_home: Path, plain_dir: Path,
) -> None:
    """If meta.json hasn't been written yet, the label falls back to `…`
    until meta lands. Tail must still attach and render events."""
    rs = RosterStore()
    rs.save(Specialist.from_template("aria", "backend"))
    ps = ProjectStore()
    proj = Project(
        id="abc123def456", name="myws", repo_path=str(plain_dir),
        kind="workspace", assigned_specialists=["aria"],
    )
    ps.save(proj)

    missions_dir = ps.missions_dir(proj.id)
    missions_dir.mkdir(parents=True, exist_ok=True)
    mid = "m-20260504-090000-aaaa"
    d = missions_dir / mid
    d.mkdir()
    # No meta.json — only an events.jsonl with one text event.
    (d / "events.jsonl").write_text(json.dumps({
        "_type": "AssistantMessage",
        "content": [{"text": "hello"}],
    }) + "\n")

    real_sleep = time.sleep
    n = {"i": 0}

    def stub_sleep(s: float) -> None:
        n["i"] += 1
        if n["i"] >= 1:
            raise KeyboardInterrupt
        real_sleep(s)

    runner = CliRunner()
    with patch("workforce.cli_project.time.sleep", side_effect=stub_sleep):
        result = runner.invoke(app, ["project", "tail", "myws"])

    assert result.exit_code == 0, result.output
    # Label is short_id + "…" since no specialist known yet.
    assert "aaaa/" in result.output  # any label works; just confirm something
    assert "hello" in result.output
