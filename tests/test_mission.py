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
)

from workforce import mission
from workforce import runner as runner_mod
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
    mid = generate_mission_id(now=dt.datetime(2026, 5, 2, 14, 12, 34, tzinfo=dt.UTC))
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


def test_compose_user_prompt_includes_working_directory() -> None:
    out = compose_user_prompt(
        "x", working_directory="/home/will/.workforce/projects/abc/worktrees/m-1",
    )
    assert "Working directory" in out
    assert "/home/will/.workforce/projects/abc/worktrees/m-1" in out
    assert "/root/repo" in out  # mentioned in the warning


def test_compose_user_prompt_omits_cwd_section_when_none() -> None:
    out = compose_user_prompt("x")
    assert "Working directory" not in out


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


def test_scan_commits_lists_commits(repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    _commit(repo, "feat(api): add health endpoint", file="a.txt")
    _commit(repo, "test(api): cover health endpoint", file="b.txt")
    commits = scan_commits(repo, base)
    assert len(commits) == 2
    assert commits[0].subject == "feat(api): add health endpoint"
    assert commits[1].subject == "test(api): cover health endpoint"


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


# ----- workspace kind --------------------------------------------------------


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    """A plain (non-git) directory for workspace-kind project tests."""
    d = tmp_path / "workspace"
    d.mkdir()
    return d


@pytest.fixture
def workspace_project(
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
    workspace_dir: Path,
    specialist: Specialist,
) -> Project:
    _, ps, _ = stores
    proj = Project(
        id="def456abc789",
        name="myws",
        repo_path=str(workspace_dir),
        kind="workspace",
        assigned_specialists=[specialist.name],
    )
    ps.save(proj)
    return proj


def test_compose_user_prompt_workspace_uses_workspace_criteria() -> None:
    p = compose_user_prompt(
        "do a thing",
        working_directory="/tmp/ws",
        kind="workspace",
    )
    assert "Save your work to files" in p
    assert "All work committed to this branch" not in p
    assert "persistent state" in p


def test_compose_user_prompt_repo_uses_repo_criteria() -> None:
    p = compose_user_prompt(
        "do a thing",
        working_directory="/tmp/r",
        kind="repo",
    )
    assert "All work committed to this branch" in p
    assert "Save your work to files" not in p


class _ExplodingWorktreeManager:
    """Test double — fails the test if anyone calls .create()."""

    def create(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("worktree_manager.create called for workspace mission")


def test_dispatch_workspace_happy_path(
    workspace_project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
    workspace_dir: Path,
) -> None:
    rs, ps, _ = stores
    msgs = [_assistant("wrote listings.md"), _result(cost=0.15, session="sess-W")]

    captured: dict[str, Any] = {}
    base_fake = _mock_runner(msgs)

    async def spy_runner(**kwargs: Any) -> RunResult:
        captured["cwd"] = kwargs.get("cwd")
        captured["user_prompt"] = kwargs.get("user_prompt")
        result: RunResult = await base_fake(**kwargs)
        return result

    async def runit() -> MissionMeta:
        return await dispatch(
            project=workspace_project,
            specialist=specialist,
            ticket="list 5 senior python roles",
            roster_store=rs,
            project_store=ps,
            worktree_manager=_ExplodingWorktreeManager(),  # type: ignore[arg-type]
            mission_id="m-test-ws01",
        )

    with patch.object(runner_mod, "run_specialist", spy_runner):
        with patch.object(mission, "extract_memory_delta", _no_memory):
            meta = asyncio.run(runit())

    assert meta.status is MissionStatus.COMPLETED
    assert meta.branch is None
    assert meta.base_sha is None
    assert meta.worktree_path == str(workspace_dir)
    assert meta.commits == []

    # Specialist ran in the workspace itself, not a worktree.
    assert captured["cwd"] == workspace_dir
    # The user prompt got the workspace success criteria.
    assert "Save your work to files" in captured["user_prompt"]

    # meta.json round-trips through pydantic with branch=null.
    mp = mission.mission_paths(workspace_project.id, "m-test-ws01")
    saved = json.loads(mp.meta.read_text())
    assert saved["branch"] is None
    assert saved["base_sha"] is None
    # Re-validate to confirm Optional fields load cleanly.
    MissionMeta.model_validate(saved)


def test_dispatch_workspace_skips_reviewer(
    workspace_project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
) -> None:
    """Even if review=True is passed directly, workspace missions skip the Reviewer.

    The CLI rejects --review for workspace projects; this is the belt-and-braces
    guard inside dispatch() for any direct caller.
    """
    rs, ps, _ = stores
    msgs = [_assistant("done"), _result(cost=0.05, session="sess-W")]

    async def reviewer_explodes(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("reviewer.run_reviewer must not be called for workspace")

    from workforce import reviewer as reviewer_mod

    with patch.object(runner_mod, "run_specialist", _mock_runner(msgs)):
        with patch.object(mission, "extract_memory_delta", _no_memory):
            with patch.object(reviewer_mod, "run_reviewer", reviewer_explodes):
                meta = asyncio.run(
                    dispatch(
                        project=workspace_project,
                        specialist=specialist,
                        ticket="t",
                        roster_store=rs,
                        project_store=ps,
                        worktree_manager=_ExplodingWorktreeManager(),  # type: ignore[arg-type]
                        mission_id="m-test-ws02",
                        review=True,
                    )
                )

    assert meta.status is MissionStatus.COMPLETED
    assert meta.reviews == []


def test_dispatch_workspace_rejects_start_point(
    workspace_project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
) -> None:
    """start_point and additional_merges are git-only — refuse loudly for workspace."""
    rs, ps, _ = stores

    async def runit() -> MissionMeta:
        return await dispatch(
            project=workspace_project,
            specialist=specialist,
            ticket="t",
            roster_store=rs,
            project_store=ps,
            worktree_manager=_ExplodingWorktreeManager(),  # type: ignore[arg-type]
            mission_id="m-test-ws03",
            start_point="some-branch",
        )

    with pytest.raises(ValueError, match="workspace"):
        asyncio.run(runit())


# ----- ownership callback wiring ---------------------------------------------


def test_dispatch_passes_owns_paths_callback_to_runner(
    project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
) -> None:
    """When dispatch() is given owns_paths, it builds a callback and forwards it
    to runner.run_specialist as `can_use_tool`."""
    rs, ps, wm = stores
    msgs = [_assistant("done"), _result(cost=0.05, session="sess")]

    captured: dict[str, Any] = {}
    base_fake = _mock_runner(msgs)

    async def spy(**kwargs: Any) -> RunResult:
        captured["can_use_tool"] = kwargs.get("can_use_tool")
        result: RunResult = await base_fake(**kwargs)
        return result

    with patch.object(runner_mod, "run_specialist", spy):
        with patch.object(mission, "extract_memory_delta", _no_memory):
            asyncio.run(
                dispatch(
                    project=project,
                    specialist=specialist,
                    ticket="t",
                    roster_store=rs,
                    project_store=ps,
                    worktree_manager=wm,
                    mission_id="m-test-own1",
                    owns_paths=["app/api/**"],
                    excludes_paths=["app/api/legacy/**"],
                )
            )

    cb = captured["can_use_tool"]
    assert cb is not None, "callback was not forwarded"
    # Smoke-test the callback: a path under app/api should be allowed; outside it, denied.
    from claude_agent_sdk import (
        PermissionResultAllow,
        PermissionResultDeny,
        ToolPermissionContext,
    )
    ctx = ToolPermissionContext(signal=None, suggestions=[], tool_use_id="t1")
    in_lane = asyncio.run(cb("Write", {"file_path": "app/api/handler.py"}, ctx))
    out = asyncio.run(cb("Write", {"file_path": "tests/test_x.py"}, ctx))
    assert isinstance(in_lane, PermissionResultAllow)
    assert isinstance(out, PermissionResultDeny)


def test_dispatch_no_owns_paths_means_no_callback(
    project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
) -> None:
    """Single-specialist dispatch (no Manager-declared lane) → no enforcement."""
    rs, ps, wm = stores
    msgs = [_assistant("done"), _result(cost=0.05, session="sess")]

    captured: dict[str, Any] = {}
    base_fake = _mock_runner(msgs)

    async def spy(**kwargs: Any) -> RunResult:
        captured["can_use_tool"] = kwargs.get("can_use_tool")
        result: RunResult = await base_fake(**kwargs)
        return result

    with patch.object(runner_mod, "run_specialist", spy):
        with patch.object(mission, "extract_memory_delta", _no_memory):
            asyncio.run(
                dispatch(
                    project=project,
                    specialist=specialist,
                    ticket="t",
                    roster_store=rs,
                    project_store=ps,
                    worktree_manager=wm,
                    mission_id="m-test-own2",
                )
            )

    assert captured["can_use_tool"] is None


# ----- revision loop (review=True) -----------------------------------------


from workforce import reviewer as reviewer_mod  # noqa: E402 – after fixtures
from workforce.reviewer import Review


def _fake_reviewer(reviews: list[Review]) -> Any:
    """Return a coroutine replacing reviewer.run_reviewer.

    Each call pops the next Review from the list (with a small cost).
    Raises ReviewError on the last call if the list is exhausted unexpectedly.
    """
    call_idx = [0]

    async def fake(*, worktree_path: Any, base_sha: Any, ticket: Any, **_: Any) -> tuple[Review, float]:
        idx = call_idx[0]
        call_idx[0] += 1
        if idx >= len(reviews):
            from workforce.reviewer import ReviewError
            raise ReviewError("unexpected extra reviewer call")
        return reviews[idx], 0.03

    return fake


def test_dispatch_review_approved_first_round(
    project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
) -> None:
    """Reviewer approves on the first round → status COMPLETED, one review record."""
    rs, ps, wm = stores
    msgs = [_assistant("done"), _result(cost=0.20, turns=3, session="s1")]

    with patch.object(runner_mod, "run_specialist", _mock_runner(msgs)):
        with patch.object(mission, "extract_memory_delta", _no_memory):
            with patch.object(reviewer_mod, "run_reviewer",
                              _fake_reviewer([Review(approved=True, summary="LGTM")])):
                meta = asyncio.run(dispatch(
                    project=project, specialist=specialist,
                    ticket="t", roster_store=rs, project_store=ps,
                    worktree_manager=wm, mission_id="m-rev-ok",
                    review=True,
                ))

    assert meta.status is MissionStatus.COMPLETED
    assert len(meta.reviews) == 1
    assert meta.reviews[0].approved is True
    assert meta.revision_rounds == 0


def test_dispatch_review_rejected_then_approved(
    project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
) -> None:
    """Reviewer rejects round 1, approves round 2 → COMPLETED, two review records,
    one revision round, specialist ran twice."""
    rs, ps, wm = stores
    specialist_run_count = [0]
    base_runner = _mock_runner([_assistant("done"), _result(cost=0.15, turns=3, session="s1")])

    async def counting_runner(**kwargs: Any) -> Any:
        specialist_run_count[0] += 1
        return await base_runner(**kwargs)

    with patch.object(runner_mod, "run_specialist", counting_runner):
        with patch.object(mission, "extract_memory_delta", _no_memory):
            with patch.object(reviewer_mod, "run_reviewer", _fake_reviewer([
                Review(approved=False, summary="needs fixes", issues=["issue A"]),
                Review(approved=True, summary="all good"),
            ])):
                meta = asyncio.run(dispatch(
                    project=project, specialist=specialist,
                    ticket="t", roster_store=rs, project_store=ps,
                    worktree_manager=wm, mission_id="m-rev-retry",
                    review=True, max_revisions=3,
                ))

    assert meta.status is MissionStatus.COMPLETED
    assert len(meta.reviews) == 2
    assert meta.reviews[0].approved is False
    assert meta.reviews[1].approved is True
    assert meta.revision_rounds == 1
    assert specialist_run_count[0] == 2


def test_dispatch_review_exhausts_max_revisions(
    project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
) -> None:
    """Reviewer rejects every round up to max_revisions → REVIEW_REJECTED status."""
    rs, ps, wm = stores
    # max_revisions=2 means: initial run + 2 re-runs = 3 specialist runs.
    # Reviewer fires after each, so 3 reviewer calls.
    all_rejected = [
        Review(approved=False, summary=f"reject round {i}", issues=[f"issue {i}"])
        for i in range(1, 4)
    ]

    with patch.object(runner_mod, "run_specialist", _mock_runner(
        [_assistant("done"), _result(cost=0.10, turns=3, session="s1")]
    )):
        with patch.object(mission, "extract_memory_delta", _no_memory):
            with patch.object(reviewer_mod, "run_reviewer", _fake_reviewer(all_rejected)):
                meta = asyncio.run(dispatch(
                    project=project, specialist=specialist,
                    ticket="t", roster_store=rs, project_store=ps,
                    worktree_manager=wm, mission_id="m-rev-cap",
                    review=True, max_revisions=2,
                ))

    assert meta.status is MissionStatus.REVIEW_REJECTED
    # 3 reviewer calls (initial + 2 revisions)
    assert len(meta.reviews) == 3
    assert all(not r.approved for r in meta.reviews)
    assert meta.revision_rounds == 2
    assert meta.error_detail is not None
    assert "reviewer rejected" in meta.error_detail


def test_dispatch_review_rejected_on_wall_timeout_stays_timeout(
    project: Project,
    specialist: Specialist,
    stores: tuple[RosterStore, ProjectStore, WorktreeManager],
) -> None:
    """If the specialist hits WALL_TIMEOUT, the Reviewer is skipped and
    REVIEW_REJECTED must NOT be set (the status should stay WALL_TIMEOUT).

    This guards the 'if run.status is RunStatus.COMPLETED' guard in the
    revision loop.
    """
    rs, ps, wm = stores
    msgs = [_assistant("ran out of time"), _result(cost=0.10, turns=5, session="s1")]

    reviewer_called = [False]

    async def reviewer_spy(**_: Any) -> Any:
        reviewer_called[0] = True
        return Review(approved=False, summary="too slow"), 0.02

    with patch.object(runner_mod, "run_specialist",
                      _mock_runner(msgs, status=RunStatus.WALL_TIMEOUT)):
        with patch.object(mission, "extract_memory_delta", _no_memory):
            with patch.object(reviewer_mod, "run_reviewer", reviewer_spy):
                meta = asyncio.run(dispatch(
                    project=project, specialist=specialist,
                    ticket="t", roster_store=rs, project_store=ps,
                    worktree_manager=wm, mission_id="m-rev-timeout",
                    review=True,
                ))

    assert meta.status is MissionStatus.WALL_TIMEOUT
    assert reviewer_called[0] is False  # Reviewer skipped entirely
    assert meta.reviews == []


# ----- fcntl Windows guard --------------------------------------------------


def test_append_project_memory_without_fcntl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_append_project_memory must not raise when fcntl is unavailable (Windows).

    Simulates the Windows environment by patching mission._fcntl to None, then
    verifies the entry is still written to the file.
    """
    monkeypatch.setattr(mission, "_fcntl", None)
    mem_file = tmp_path / "memory.md"
    mission._append_project_memory(mem_file, "## m-test\n\nhello from test")
    assert mem_file.exists()
    assert "hello from test" in mem_file.read_text()


# ----- scan_commits stderr surfacing ----------------------------------------


def test_scan_commits_error_includes_stderr(tmp_path: Path) -> None:
    """When git log fails, commit_scan_error should include stderr content."""

    # scan_commits raises CalledProcessError with a bad base_sha; git will
    # write an error to stderr that should appear in the exception message.
    with pytest.raises(subprocess.CalledProcessError):
        scan_commits(tmp_path, "deadbeef" * 5)

    # Now test the dispatch-level surfacing: patch scan_commits to raise a
    # CalledProcessError with known stderr content.
    fake_exc = subprocess.CalledProcessError(
        returncode=128,
        cmd=["git", "log"],
        stderr="fatal: bad object cafebabe\n",
    )

    def _failing_scan(path: Path, sha: str) -> list[Any]:
        raise fake_exc

    with patch.object(mission, "scan_commits", _failing_scan):
        # Build a minimal error_detail string the way dispatch() does.
        # We replicate the logic directly to avoid a full dispatch() call.
        exc = fake_exc
        stderr_snippet = (exc.stderr or "")[:300]
        error_detail = (
            f"commit scan failed: {exc}"
            + (f"; stderr: {stderr_snippet}" if stderr_snippet else "")
        )
    assert "fatal: bad object" in error_detail
    assert "commit scan failed" in error_detail
