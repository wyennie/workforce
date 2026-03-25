"""Tests for the mission orchestrator.

We mock at the SDK boundary (`runner.run_specialist` and
`mission.extract_memory_delta`) and use real git, real stores, real worktrees.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
)

from workforce import mission, runner as runner_mod
from workforce.mission import (
    MemoryDelta,
    MissionMeta,
    MissionStatus,
    compose_system_prompt,
    compose_user_prompt,
    dispatch,
    generate_mission_id,
    parse_memory_delta,
    scan_commits,
)
from workforce.project import Project, ProjectStore
from workforce.runner import RunResult, RunStatus
from workforce.specialist import RosterStore, Specialist
from workforce.worktree import WorktreeManager


# ----- fixtures --------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=r, check=True)
    (r / "README.md").write_text("# t\n")
    subprocess.run(["git", "add", "README.md"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=r, check=True)
    return r


@pytest.fixture
def stores(isolated_home: Path) -> tuple[RosterStore, ProjectStore, WorktreeManager]:
    isolated_home.mkdir(parents=True, exist_ok=True)
    return RosterStore(), ProjectStore(), WorktreeManager()


@pytest.fixture
def specialist(stores: tuple[RosterStore, ProjectStore, WorktreeManager]) -> Specialist:
    rs, _, _ = stores
    spec = Specialist.from_template("aria", "backend")
    rs.save(spec)
    return spec


@pytest.fixture
def project(
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
    repo: Path,
    specialist: Specialist,
) -> Project:
    _, ps, _ = stores
    proj = Project(
        id="abc123def456",
        name="myapp",
        repo_path=str(repo),
        assigned_specialists=[specialist.name],
    )
    ps.save(proj)
    return proj


# ----- utilities -------------------------------------------------------------


def _result(*, is_error: bool = False, cost: float = 0.10, turns: int = 3, session: str = "s1") -> ResultMessage:
    return ResultMessage(
        subtype="success" if not is_error else "error",
        duration_ms=1500,
        duration_api_ms=1200,
        is_error=is_error,
        num_turns=turns,
        session_id=session,
        stop_reason=None,
        total_cost_usd=cost,
        usage=None,
        result=None,
        structured_output=None,
        model_usage=None,
        permission_denials=None,
        errors=None,
        uuid=None,
    )


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-6",
        parent_tool_use_id=None,
        error=None,
        usage=None,
        message_id="m",
        stop_reason=None,
        session_id="s1",
        uuid=None,
    )


def _commit(repo_or_worktree: Path, msg: str, *, file: str = "x.txt", content: str = "x\n") -> str:
    (repo_or_worktree / file).write_text(content)
    subprocess.run(["git", "add", file], cwd=repo_or_worktree, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=repo_or_worktree, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_or_worktree, capture_output=True, text=True, check=True
    ).stdout.strip()


# ----- ID generation ---------------------------------------------------------


def test_mission_id_format() -> None:
    mid = generate_mission_id(now=dt.datetime(2026, 5, 2, 14, 12, 34, tzinfo=dt.timezone.utc))
    assert mid.startswith("m-20260502-141234-")
    assert re.fullmatch(r"m-\d{8}-\d{6}-[0-9a-f]{4}", mid)


def test_mission_ids_are_unique() -> None:
    ids = {generate_mission_id() for _ in range(20)}
    assert len(ids) == 20


# ----- prompt composition ----------------------------------------------------


def test_compose_system_prompt_includes_base() -> None:
    spec = Specialist.from_template("aria", "backend")
    out = compose_system_prompt(spec, cross_project_memory="", project_memory="")
    assert spec.base_prompt.rstrip() in out


def test_compose_system_prompt_omits_empty_memory() -> None:
    spec = Specialist.from_template("aria", "backend")
    out = compose_system_prompt(spec, cross_project_memory="   ", project_memory="\n")
    assert "<cross_project_memory>" not in out
    assert "<project_memory>" not in out


def test_compose_system_prompt_includes_memory_when_present() -> None:
    spec = Specialist.from_template("aria", "backend")
    out = compose_system_prompt(
        spec, cross_project_memory="cross-lesson", project_memory="proj-lesson"
    )
    assert "<cross_project_memory>" in out
    assert "cross-lesson" in out
    assert "<project_memory>" in out
    assert "proj-lesson" in out


def test_compose_user_prompt_has_ticket_and_criteria() -> None:
    out = compose_user_prompt("add /health endpoint")
    assert "add /health endpoint" in out
    assert "Success criteria" in out


# ----- memory delta parsing --------------------------------------------------


def test_parse_memory_delta_fenced_json() -> None:
    text = (
        "Here you go:\n```json\n"
        '{"summary":"did x","project_memory":"a","cross_project_memory":"b"}\n'
        "```"
    )
    delta = parse_memory_delta(text)
    assert delta is not None
    assert delta.summary == "did x"
    assert delta.project_memory == "a"
    assert delta.cross_project_memory == "b"


def test_parse_memory_delta_unfenced_json() -> None:
    text = '{"summary":"x","project_memory":"","cross_project_memory":""}'
    delta = parse_memory_delta(text)
    assert delta is not None
    assert delta.summary == "x"


def test_parse_memory_delta_extra_fields_ignored() -> None:
    text = '```json\n{"summary":"x","wat":42}\n```'
    delta = parse_memory_delta(text)
    assert delta is not None
    assert delta.summary == "x"


def test_parse_memory_delta_garbage_returns_none() -> None:
    assert parse_memory_delta("not json at all") is None
    assert parse_memory_delta("") is None
    assert parse_memory_delta("```json\n{not valid\n```") is None


def test_parse_memory_delta_uses_last_block_when_multiple() -> None:
    text = (
        "first attempt:\n```json\n{\"summary\":\"old\"}\n```\n"
        "actually:\n```json\n{\"summary\":\"new\"}\n```"
    )
    delta = parse_memory_delta(text)
    assert delta is not None
    assert delta.summary == "new"


# ----- commit scanning -------------------------------------------------------


def test_scan_commits_no_violations(repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    _commit(repo, "feat(api): add health endpoint", file="a.txt")
    _commit(repo, "test(api): cover health endpoint", file="b.txt")
    commits = scan_commits(repo, base)
    assert len(commits) == 2
    assert all(not c.trailer_violations for c in commits)
    assert commits[0].subject == "feat(api): add health endpoint"


def test_scan_commits_detects_coauthor_trailer(repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    _commit(
        repo,
        "feat(api): add health endpoint\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
    )
    commits = scan_commits(repo, base)
    assert len(commits) == 1
    assert "claude-coauthor-trailer" in commits[0].trailer_violations


def test_scan_commits_detects_generated_with_line(repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    _commit(repo, "feat: x\n\n🤖 Generated with Claude Code")
    commits = scan_commits(repo, base)
    assert "claude-code-attribution" in commits[0].trailer_violations


def test_scan_commits_human_coauthor_is_fine(repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    _commit(repo, "feat: x\n\nCo-Authored-By: Alice <alice@example.com>")
    commits = scan_commits(repo, base)
    assert commits[0].trailer_violations == []


# ----- dispatch end-to-end (with mocked runner & memory call) ----------------


def _mock_runner(messages: list[Any], status: RunStatus = RunStatus.COMPLETED) -> Any:
    """Return a coroutine to use as the runner replacement.

    Calls the on_message callback with each message, writes them to events_log,
    derives cost/turns from the ResultMessage so the mock matches real behavior.
    """
    async def fake_run_specialist(**kwargs: Any) -> RunResult:
        log = kwargs.get("events_log")
        cb = kwargs.get("on_message")
        for m in messages:
            if cb:
                cb(m)
        if log:
            log.write_text("\n".join(str(type(m).__name__) for m in messages) + "\n")
        final = next((m for m in reversed(messages) if isinstance(m, ResultMessage)), None)
        return RunResult(
            status=status,
            final=final,
            cost_usd=(final.total_cost_usd or 0.0) if final else 0.0,
            duration_seconds=1.0,
            turn_count=final.num_turns if final else 0,
            error_detail=None if status is RunStatus.COMPLETED else "test error",
        )

    return fake_run_specialist


async def _no_memory(**_: Any) -> tuple[MemoryDelta | None, float]:
    return None, 0.0


async def _delta(**_: Any) -> tuple[MemoryDelta | None, float]:
    return MemoryDelta(
        summary="implemented /health",
        project_memory="health endpoint lives in app/routes.py",
        cross_project_memory="prefer aiohttp over flask in this codebase",
    ), 0.02


def test_dispatch_completed_path(
    project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
) -> None:
    rs, ps, wm = stores
    msgs = [_assistant("did the work"), _result(cost=0.20, turns=4, session="sess-X")]

    async def runit() -> MissionMeta:
        return await dispatch(
            project=project,
            specialist=specialist,
            ticket="add /health endpoint",
            roster_store=rs,
            project_store=ps,
            worktree_manager=wm,
            mission_id="m-test-aaaa",
        )

    with patch.object(runner_mod, "run_specialist", _mock_runner(msgs)):
        with patch.object(mission, "extract_memory_delta", _delta):
            meta = asyncio.run(runit())

    assert meta.status is MissionStatus.COMPLETED
    assert meta.cost_usd == pytest.approx(0.20 + 0.02)
    assert meta.turn_count == 4
    assert meta.memory_delta_captured is True

    # Mission directory present with all expected files
    mp = mission.mission_paths(project.id, "m-test-aaaa")
    assert mp.ticket.read_text().strip() == "add /health endpoint"
    assert "implemented /health" in mp.result.read_text()
    assert "did the work" in mp.transcript.read_text()
    saved = json.loads(mp.meta.read_text())
    assert saved["status"] == "completed"
    assert saved["specialist"] == specialist.name

    # Memory was appended
    cross = rs.load_memory(specialist.name)
    assert "prefer aiohttp" in cross
    assert "m-test-aaaa" in cross  # entry header
    proj_mem = (ps.memory_dir(project.id) / f"{specialist.name}.md").read_text()
    assert "health endpoint lives in app/routes.py" in proj_mem

    # Stats updated
    stats = rs.load_stats(specialist.name)
    assert stats.missions_completed == 1
    assert stats.missions_failed == 0
    assert stats.total_cost_usd == pytest.approx(0.22)


def test_dispatch_runner_error_path(
    project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
) -> None:
    rs, ps, wm = stores
    msgs = [_assistant("oh no"), _result(is_error=True, cost=0.05)]

    async def runit() -> MissionMeta:
        return await dispatch(
            project=project,
            specialist=specialist,
            ticket="t",
            roster_store=rs,
            project_store=ps,
            worktree_manager=wm,
            mission_id="m-test-bbbb",
        )

    with patch.object(runner_mod, "run_specialist", _mock_runner(msgs, status=RunStatus.ERROR)):
        with patch.object(mission, "extract_memory_delta", _no_memory):
            meta = asyncio.run(runit())

    assert meta.status is MissionStatus.ERROR
    assert meta.error_detail == "test error"
    # No memory delta attempted on errors
    assert meta.memory_delta_captured is False
    # Stats: failed
    stats = rs.load_stats(specialist.name)
    assert stats.missions_failed == 1
    assert stats.missions_completed == 0


def test_dispatch_detects_trailer_violation(
    project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
    tmp_path: Path,
) -> None:
    """Specialist 'commits' (we simulate) include a forbidden trailer."""
    rs, ps, wm = stores
    msgs = [_assistant("done"), _result(cost=0.10, session="sess-X")]

    # Wrap mock_runner so it commits a bad message in the worktree before returning.
    base_fake = _mock_runner(msgs)

    async def fake_run_specialist(**kwargs: Any) -> RunResult:
        cwd = kwargs["cwd"]
        subprocess.run(["git", "config", "user.email", "x@x"], cwd=cwd, check=True)
        subprocess.run(["git", "config", "user.name", "x"], cwd=cwd, check=True)
        (cwd / "f.txt").write_text("x\n")
        subprocess.run(["git", "add", "f.txt"], cwd=cwd, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m",
             "feat: add f\n\nCo-Authored-By: Claude <noreply@anthropic.com>"],
            cwd=cwd, check=True,
        )
        result: RunResult = await base_fake(**kwargs)
        return result

    with patch.object(runner_mod, "run_specialist", fake_run_specialist):
        with patch.object(mission, "extract_memory_delta", _no_memory):
            meta = asyncio.run(
                dispatch(
                    project=project,
                    specialist=specialist,
                    ticket="t",
                    roster_store=rs,
                    project_store=ps,
                    worktree_manager=wm,
                    mission_id="m-test-cccc",
                )
            )

    assert meta.status is MissionStatus.TRAILER_VIOLATION
    assert any("claude-coauthor-trailer" in c.trailer_violations for c in meta.commits)
    assert meta.error_detail is not None
    assert "Claude trailer" in meta.error_detail
    # Counted as a failed mission for stats purposes
    assert rs.load_stats(specialist.name).missions_failed == 1


def test_dispatch_skips_memory_call_when_no_session(
    project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
) -> None:
    """If the runner returns no session id, we skip the follow-up entirely."""
    rs, ps, wm = stores

    async def runner_no_session(**kwargs: Any) -> RunResult:
        cb = kwargs.get("on_message")
        msg = _result(cost=0.05, session="")
        msg.session_id = ""  # explicitly empty
        if cb:
            cb(msg)
        return RunResult(
            status=RunStatus.COMPLETED,
            final=msg,
            cost_usd=0.05,
            duration_seconds=1.0,
            turn_count=1,
        )

    delta_called = False

    async def delta_spy(**_: Any) -> tuple[MemoryDelta | None, float]:
        nonlocal delta_called
        delta_called = True
        return None, 0.0

    with patch.object(runner_mod, "run_specialist", runner_no_session):
        with patch.object(mission, "extract_memory_delta", delta_spy):
            asyncio.run(
                dispatch(
                    project=project,
                    specialist=specialist,
                    ticket="t",
                    roster_store=rs,
                    project_store=ps,
                    worktree_manager=wm,
                    mission_id="m-test-dddd",
                )
            )
    assert delta_called is False
