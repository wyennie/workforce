"""Tests for workforce.parallel: orchestration with mocked manager + sub-runs."""

from __future__ import annotations

import asyncio
import json
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

from workforce import manager, mission, parallel, runner as runner_mod
from workforce.manager import Contract, Decomposition, DecompositionKind, Task
from workforce.mission import MissionMeta, MissionStatus
from workforce.parallel import (
    AutoMergeStepResult,
    MergeStep,
    ParallelStatus,
    ResolutionError,
    auto_merge,
    dispatch_parallel,
    merge_plan,
    resolve_task_specialists,
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
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "src").mkdir()
    (r / "src" / "main.py").write_text("# initial\n")
    subprocess.run(["git", "add", "-A"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


@pytest.fixture
def stores_and_project(
    isolated_home: Path, repo: Path
) -> tuple[RosterStore, ProjectStore, WorktreeManager, Project]:
    isolated_home.mkdir(parents=True, exist_ok=True)
    rs = RosterStore()
    ps = ProjectStore()
    wm = WorktreeManager()
    rs.save(Specialist.from_template("aria", "backend"))
    rs.save(Specialist.from_template("ben", "frontend"))
    rs.save(Specialist.from_template("casey", "tester"))
    proj = Project(
        id="abc123def456",
        name="myapp",
        repo_path=str(repo),
        assigned_specialists=["aria", "ben", "casey"],
    )
    ps.save(proj)
    return rs, ps, wm, proj


# ----- specialist resolution -------------------------------------------------


def _decomp_three_tasks() -> Decomposition:
    return Decomposition(
        ticket="t",
        kind=DecompositionKind.PARALLEL,
        rationale="r",
        contract=Contract(needed=True, path="c.md", body="API contract..."),
        tasks=[
            Task(
                id="impl",
                description="impl it",
                owns_paths=["src/auth/**"],
                depends_on=["contract"],
                suggested_specialist="aria",
            ),
            Task(
                id="tests",
                description="test it",
                owns_paths=["tests/auth/**"],
                depends_on=["contract"],
                suggested_specialist="casey",
            ),
            Task(
                id="docs",
                description="document it",
                owns_paths=["README.md"],
                depends_on=["contract"],
                suggested_specialist="ben",
            ),
        ],
        merge_order=["impl", "tests", "docs"],
    )


def test_resolve_uses_suggested(stores_and_project: tuple[RosterStore, ProjectStore, WorktreeManager, Project]) -> None:
    rs, ps, _, proj = stores_and_project
    decomp = _decomp_three_tasks()
    resolved = resolve_task_specialists(
        decomp, parent_mission_id="m-x", project=proj, roster_store=rs,
        project_store=ps,
    )
    by_task = {r.task.id: r.specialist.name for r in resolved}
    assert by_task == {"impl": "aria", "tests": "casey", "docs": "ben"}
    assert all(r.staffing_action == "already_assigned" for r in resolved)


def test_resolve_auto_assigns_existing_roster_member(
    stores_and_project: tuple[RosterStore, ProjectStore, WorktreeManager, Project],
) -> None:
    """Manager suggests an existing specialist who isn't on the project yet."""
    rs, ps, _, proj = stores_and_project
    rs.save(Specialist.from_template("dana", "generalist"))  # not assigned
    decomp = _decomp_three_tasks()
    decomp.tasks[0].suggested_specialist = "dana"

    resolved = resolve_task_specialists(
        decomp, parent_mission_id="m-x", project=proj,
        roster_store=rs, project_store=ps,
    )
    impl = next(r for r in resolved if r.task.id == "impl")
    assert impl.specialist.name == "dana"
    assert impl.staffing_action == "auto_assigned_from_roster"
    refreshed = ps.load_by_id(proj.id)
    assert "dana" in refreshed.assigned_specialists


def test_resolve_auto_hires_from_template(
    stores_and_project: tuple[RosterStore, ProjectStore, WorktreeManager, Project],
) -> None:
    rs, ps, _, proj = stores_and_project
    decomp = _decomp_three_tasks()
    decomp.tasks[0].suggested_specialist = "migration-aria"
    decomp.tasks[0].template_hint = "backend"

    resolved = resolve_task_specialists(
        decomp, parent_mission_id="m-x", project=proj,
        roster_store=rs, project_store=ps,
    )
    impl = next(r for r in resolved if r.task.id == "impl")
    assert impl.specialist.name == "migration-aria"
    assert impl.staffing_action == "auto_hired_from_template"
    assert rs.exists("migration-aria")
    refreshed = ps.load_by_id(proj.id)
    assert "migration-aria" in refreshed.assigned_specialists


def test_resolve_auto_hire_unknown_template_errors(
    stores_and_project: tuple[RosterStore, ProjectStore, WorktreeManager, Project],
) -> None:
    rs, ps, _, proj = stores_and_project
    decomp = _decomp_three_tasks()
    decomp.tasks[0].suggested_specialist = "newcomer"
    decomp.tasks[0].template_hint = "no-such-template"
    with pytest.raises(ResolutionError, match="doesn't exist"):
        resolve_task_specialists(
            decomp, parent_mission_id="m-x", project=proj,
            roster_store=rs, project_store=ps,
        )


def test_resolve_no_auto_staff_falls_back(
    stores_and_project: tuple[RosterStore, ProjectStore, WorktreeManager, Project],
) -> None:
    """With auto_staff=False, an unassigned roster member falls through."""
    rs, ps, _, proj = stores_and_project
    rs.save(Specialist.from_template("dana", "generalist"))
    decomp = _decomp_three_tasks()
    decomp.tasks[0].suggested_specialist = "dana"

    resolved = resolve_task_specialists(
        decomp, parent_mission_id="m-x", project=proj,
        roster_store=rs, project_store=ps,
        fallback_specialist="aria", auto_staff=False,
    )
    impl = next(r for r in resolved if r.task.id == "impl")
    assert impl.specialist.name == "aria"
    assert impl.staffing_action == "fallback"
    refreshed = ps.load_by_id(proj.id)
    assert "dana" not in refreshed.assigned_specialists


def test_resolve_falls_back_when_suggestion_unknown_no_template(
    stores_and_project: tuple[RosterStore, ProjectStore, WorktreeManager, Project],
) -> None:
    rs, ps, _, proj = stores_and_project
    decomp = _decomp_three_tasks()
    decomp.tasks[0].suggested_specialist = "ghost"  # not in roster, no template hint
    resolved = resolve_task_specialists(
        decomp, parent_mission_id="m-x", project=proj,
        roster_store=rs, project_store=ps,
        fallback_specialist="aria",
    )
    impl = next(r for r in resolved if r.task.id == "impl")
    assert impl.specialist.name == "aria"
    assert impl.staffing_action == "fallback"


def test_resolve_errors_when_no_suggestion_and_no_fallback(
    stores_and_project: tuple[RosterStore, ProjectStore, WorktreeManager, Project],
) -> None:
    rs, ps, _, proj = stores_and_project
    decomp = _decomp_three_tasks()
    decomp.tasks[0].suggested_specialist = None
    with pytest.raises(ResolutionError, match="cannot resolve"):
        resolve_task_specialists(
            decomp, parent_mission_id="m-x", project=proj,
            roster_store=rs, project_store=ps,
        )


def test_resolve_uses_only_assigned_when_one_specialist(
    isolated_home: Path, repo: Path
) -> None:
    isolated_home.mkdir(parents=True, exist_ok=True)
    rs = RosterStore()
    rs.save(Specialist.from_template("solo", "backend"))
    ps = ProjectStore()
    proj = Project(id="aaa111111111", name="x", repo_path=str(repo), assigned_specialists=["solo"])
    ps.save(proj)
    decomp = Decomposition(
        ticket="t", kind=DecompositionKind.SINGLE, rationale="r",
        tasks=[Task(id="solo", description="x")],
        merge_order=["solo"],
    )
    resolved = resolve_task_specialists(
        decomp, parent_mission_id="m-x", project=proj, roster_store=rs,
        project_store=ps,
    )
    assert resolved[0].specialist.name == "solo"


# ----- merge plan ------------------------------------------------------------


def _meta(mid: str, status: MissionStatus = MissionStatus.COMPLETED, branch: str = "") -> MissionMeta:
    return MissionMeta(
        mission_id=mid,
        project_id="abc123def456",
        project_name="myapp",
        specialist="aria",
        model="m",
        ticket="t",
        branch=branch or f"workforce/{mid}",
        worktree_path=f"/tmp/{mid}",
        base_sha="abc1234",
        started_at="2026-05-02T00:00:00Z",
        ended_at="2026-05-02T00:01:00Z",
        duration_seconds=60.0,
        status=status,
    )


def test_merge_plan_respects_order() -> None:
    parent = parallel.ParallelMissionMeta(
        parent_mission_id="m-p",
        project_id="abc123def456",
        project_name="myapp",
        ticket="t",
        started_at="x",
        status=ParallelStatus.COMPLETED,
        decomposition_kind=DecompositionKind.PARALLEL,
        merge_order=["impl", "tests", "docs"],
        sub_missions=[
            parallel.SubMissionRef(task_id=t, mission_id=f"m-p__{t}", specialist="aria")
            for t in ["impl", "tests", "docs"]
        ],
    )
    subs = [_meta(f"m-p__{t}") for t in ["impl", "tests", "docs"]]
    plan = merge_plan(parent, subs)
    assert [s.task_id for s in plan] == ["impl", "tests", "docs"]


def test_merge_plan_includes_failed_with_status() -> None:
    parent = parallel.ParallelMissionMeta(
        parent_mission_id="m-p",
        project_id="abc123def456",
        project_name="myapp",
        ticket="t",
        started_at="x",
        status=ParallelStatus.PARTIAL,
        decomposition_kind=DecompositionKind.PARALLEL,
        merge_order=["impl", "tests"],
        sub_missions=[
            parallel.SubMissionRef(task_id=t, mission_id=f"m-p__{t}", specialist="aria")
            for t in ["impl", "tests"]
        ],
    )
    subs = [
        _meta("m-p__impl"),
        _meta("m-p__tests", status=MissionStatus.ERROR),
    ]
    plan = merge_plan(parent, subs)
    statuses = [s.status for s in plan]
    assert statuses == [MissionStatus.COMPLETED, MissionStatus.ERROR]


# ----- dispatch_parallel end-to-end (mocked manager + runner) ----------------


def _fake_manager(decomp: Decomposition) -> Any:
    """Replace manager.run_manager with a coroutine returning a canned decomp."""
    async def fake(**_: Any) -> tuple[Decomposition, float, list[Any]]:
        return decomp, 0.05, []
    return fake


def _fake_runner(cost: float = 0.10) -> Any:
    """Replace runner.run_specialist with a coroutine that returns success."""
    async def fake(**kwargs: Any) -> RunResult:
        cb = kwargs.get("on_message")
        result = ResultMessage(
            subtype="success", duration_ms=1000, duration_api_ms=900,
            is_error=False, num_turns=3, session_id=f"s-{kwargs.get('cwd')}",
            stop_reason=None, total_cost_usd=cost, usage=None, result=None,
            structured_output=None, model_usage=None, permission_denials=None,
            errors=None, uuid=None,
        )
        if cb:
            cb(AssistantMessage(
                content=[TextBlock(text="done")],
                model="m", parent_tool_use_id=None, error=None, usage=None,
                message_id="x", stop_reason=None, session_id="s", uuid=None,
            ))
            cb(result)
        return RunResult(
            status=RunStatus.COMPLETED, final=result, cost_usd=cost,
            duration_seconds=1.0, turn_count=3,
        )
    return fake


async def _no_memory(**_: Any) -> tuple[Any, float]:
    return None, 0.0


def test_dispatch_parallel_full_flow(
    stores_and_project: tuple[RosterStore, ProjectStore, WorktreeManager, Project],
) -> None:
    rs, ps, wm, proj = stores_and_project
    decomp = _decomp_three_tasks()

    captured_calls: list[str] = []
    def confirm(d: Decomposition, resolved: list[tuple[str, str, str]]) -> bool:
        captured_calls.append("confirm")
        return True

    with patch.object(manager, "run_manager", _fake_manager(decomp)):
        with patch.object(runner_mod, "run_specialist", _fake_runner()):
            with patch.object(mission, "extract_memory_delta", _no_memory):
                result = asyncio.run(
                    dispatch_parallel(
                        project=proj,
                        ticket="refactor auth",
                        roster_store=rs,
                        project_store=ps,
                        worktree_manager=wm,
                        confirm=confirm,
                        parent_mission_id="m-test-parent",
                    )
                )

    assert captured_calls == ["confirm"]
    assert result.parent_meta.status is ParallelStatus.COMPLETED
    assert len(result.sub_metas) == 3
    assert all(m.status is MissionStatus.COMPLETED for m in result.sub_metas)

    # Decomposition + contract written to parent dir
    parent_dir = mission.mission_paths(proj.id, "m-test-parent").root
    assert (parent_dir / "decomposition.json").is_file()
    assert (parent_dir / "contract" / "contract.md").is_file()
    assert (parent_dir / "meta.json").is_file()

    parent_meta = json.loads((parent_dir / "meta.json").read_text())
    assert parent_meta["status"] == "completed"
    assert len(parent_meta["sub_missions"]) == 3
    assert parent_meta["sub_missions"][0]["mission_id"] == "m-test-parent__impl"

    # Each sub-mission has its own dir
    for task_id in ["impl", "tests", "docs"]:
        sub_dir = mission.mission_paths(proj.id, f"m-test-parent__{task_id}").root
        assert sub_dir.is_dir()
        assert (sub_dir / "meta.json").is_file()

    # Contract content was injected into each sub-mission's user prompt — we
    # can't directly verify without inspecting the runner kwargs, but each
    # sub-mission should have completed successfully so we infer it didn't crash.


def test_dispatch_parallel_user_cancels(
    stores_and_project: tuple[RosterStore, ProjectStore, WorktreeManager, Project],
) -> None:
    rs, ps, wm, proj = stores_and_project
    decomp = _decomp_three_tasks()

    with patch.object(manager, "run_manager", _fake_manager(decomp)):
        with patch.object(runner_mod, "run_specialist", _fake_runner()):
            result = asyncio.run(
                dispatch_parallel(
                    project=proj,
                    ticket="refactor auth",
                    roster_store=rs,
                    project_store=ps,
                    worktree_manager=wm,
                    confirm=lambda _d, _r: False,
                    parent_mission_id="m-test-cancel",
                )
            )

    assert result.parent_meta.status is ParallelStatus.CANCELLED
    assert result.sub_metas == []
    # Decomposition was still saved for the user's reference
    parent_dir = mission.mission_paths(proj.id, "m-test-cancel").root
    assert (parent_dir / "decomposition.json").is_file()


def test_dispatch_parallel_partial_success(
    stores_and_project: tuple[RosterStore, ProjectStore, WorktreeManager, Project],
) -> None:
    """One sub-mission errors; others succeed."""
    rs, ps, wm, proj = stores_and_project
    decomp = _decomp_three_tasks()

    async def mixed_runner(**kwargs: Any) -> RunResult:
        cwd = str(kwargs["cwd"])
        is_failure = "tests" in cwd
        result = ResultMessage(
            subtype="error" if is_failure else "success",
            duration_ms=1000, duration_api_ms=900, is_error=is_failure,
            num_turns=3, session_id="s", stop_reason=None,
            total_cost_usd=0.05, usage=None, result=None,
            structured_output=None, model_usage=None, permission_denials=None,
            errors=["boom"] if is_failure else None, uuid=None,
        )
        return RunResult(
            status=RunStatus.ERROR if is_failure else RunStatus.COMPLETED,
            final=result, cost_usd=0.05, duration_seconds=1.0, turn_count=3,
            error_detail="boom" if is_failure else None,
        )

    with patch.object(manager, "run_manager", _fake_manager(decomp)):
        with patch.object(runner_mod, "run_specialist", mixed_runner):
            with patch.object(mission, "extract_memory_delta", _no_memory):
                result = asyncio.run(
                    dispatch_parallel(
                        project=proj,
                        ticket="refactor auth",
                        roster_store=rs,
                        project_store=ps,
                        worktree_manager=wm,
                        confirm=lambda _d, _r: True,
                        parent_mission_id="m-test-partial",
                    )
                )

    assert result.parent_meta.status is ParallelStatus.PARTIAL
    statuses = sorted(m.status.value for m in result.sub_metas)
    assert statuses == ["completed", "completed", "error"]


def test_dispatch_parallel_validation_failure(
    stores_and_project: tuple[RosterStore, ProjectStore, WorktreeManager, Project],
) -> None:
    """Manager produces an invalid decomposition (overlapping paths)."""
    rs, ps, wm, proj = stores_and_project

    # Both tasks claim the same files
    (Path(proj.repo_path) / "src" / "main.py").write_text("# x")
    bad = Decomposition(
        ticket="t",
        kind=DecompositionKind.PARALLEL,
        rationale="bad",
        contract=Contract(needed=True, path="c.md", body="..."),
        tasks=[
            Task(
                id="a", description="x", owns_paths=["src/**"], depends_on=["contract"],
                suggested_specialist="aria",
            ),
            Task(
                id="b", description="y", owns_paths=["src/**"], depends_on=["contract"],
                suggested_specialist="ben",
            ),
        ],
        merge_order=["a", "b"],
    )

    with patch.object(manager, "run_manager", _fake_manager(bad)):
        with pytest.raises(manager.ValidationError, match="both claim files"):
            asyncio.run(
                dispatch_parallel(
                    project=proj,
                    ticket="t",
                    roster_store=rs,
                    project_store=ps,
                    worktree_manager=wm,
                    parent_mission_id="m-test-bad",
                )
            )

    # Decomposition was still saved for the user's reference
    parent_dir = mission.mission_paths(proj.id, "m-test-bad").root
    assert (parent_dir / "decomposition.json").is_file()


# ----- auto-merge ------------------------------------------------------------


def _make_branch(repo: Path, branch: str, file: str, content: str) -> None:
    """Create a branch off main with one commit modifying `file`."""
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=repo, check=True)
    (repo / file).write_text(content)
    subprocess.run(["git", "add", file], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"feat: {branch}"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)


def _step(task_id: str, branch: str, status: MissionStatus = MissionStatus.COMPLETED) -> MergeStep:
    return MergeStep(task_id=task_id, branch=branch, sub_mission_id=f"m__{task_id}", status=status)


def test_auto_merge_clean_path(repo: Path) -> None:
    _make_branch(repo, "wf/a", "a.txt", "hi from a\n")
    _make_branch(repo, "wf/b", "b.txt", "hi from b\n")
    plan = [_step("a", "wf/a"), _step("b", "wf/b")]
    results = auto_merge(repo, plan)
    assert all(r.success for r in results)
    assert (repo / "a.txt").read_text() == "hi from a\n"
    assert (repo / "b.txt").read_text() == "hi from b\n"
    # Two merge commits on top of main
    log = subprocess.run(
        ["git", "log", "--oneline", "main"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "Merge branch 'wf/a'" in log
    assert "Merge branch 'wf/b'" in log


def test_auto_merge_aborts_on_conflict_and_skips_rest(repo: Path) -> None:
    """Two branches modifying the same file → second merge conflicts."""
    _make_branch(repo, "wf/a", "shared.txt", "from a\n")
    _make_branch(repo, "wf/b", "shared.txt", "from b\n")
    _make_branch(repo, "wf/c", "c.txt", "fine\n")
    plan = [_step("a", "wf/a"), _step("b", "wf/b"), _step("c", "wf/c")]
    results = auto_merge(repo, plan)
    assert results[0].success
    assert not results[1].success
    assert "conflict" in results[1].detail.lower() or "merge error" in results[1].detail.lower()
    assert not results[2].success
    assert "skipped" in results[2].detail.lower()
    # Repo should not be in the middle of an unresolved merge
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    assert "UU" not in status  # no unmerged paths


def test_auto_merge_skips_non_completed_steps(repo: Path) -> None:
    _make_branch(repo, "wf/a", "a.txt", "hi\n")
    plan = [
        _step("a", "wf/a"),
        _step("b", "wf/b-doesnt-exist", status=MissionStatus.ERROR),
    ]
    results = auto_merge(repo, plan)
    assert results[0].success
    assert not results[1].success
    assert "skipped" in results[1].detail.lower()


def test_auto_merge_missing_branch_aborts(repo: Path) -> None:
    plan = [_step("a", "wf/does-not-exist")]
    results = auto_merge(repo, plan)
    assert not results[0].success
    assert "merge error" in results[0].detail.lower() or "conflict" in results[0].detail.lower()
