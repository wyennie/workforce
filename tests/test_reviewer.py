"""Tests for workforce.reviewer: model + parser + revision loop integration."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import (
    ResultMessage,
)

from workforce import mission, reviewer
from workforce import runner as runner_mod
from workforce.mission import MissionStatus, dispatch
from workforce.project import Project, ProjectStore
from workforce.reviewer import (
    Review,
    ReviewError,
    diff_stats,
    parse_review,
)
from workforce.runner import RunResult, RunStatus
from workforce.specialist import RosterStore, Specialist
from workforce.worktree import WorktreeManager

# ----- Model ----------------------------------------------------------------


def test_review_model_extra_fields_ignored() -> None:
    r = Review.model_validate(
        {"approved": True, "summary": "x", "issues": [], "wat": True}
    )
    assert r.approved is True
    assert r.summary == "x"


# ----- parse_review ---------------------------------------------------------


def test_parse_review_fenced() -> None:
    text = """\
Looked at the diff. Tests pass. Approved.
```json
{"schema_version": 1, "approved": true, "summary": "ok", "issues": []}
```
"""
    r = parse_review(text)
    assert r.approved is True
    assert r.summary == "ok"


def test_parse_review_unfenced() -> None:
    r = parse_review('{"approved": false, "summary": "broken", "issues": ["a", "b"]}')
    assert r.approved is False
    assert r.issues == ["a", "b"]


def test_parse_review_uses_last_fenced_when_multiple() -> None:
    text = (
        '```json\n{"approved": false, "summary": "early"}\n```\n'
        '```json\n{"approved": true, "summary": "late"}\n```'
    )
    r = parse_review(text)
    assert r.summary == "late"


def test_parse_review_garbage_raises() -> None:
    with pytest.raises(ReviewError):
        parse_review("not json")
    with pytest.raises(ReviewError):
        parse_review("```json\n{bad\n```")


# ----- diff_stats -----------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "README.md").write_text("# r\n")
    subprocess.run(["git", "add", "README.md"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


def test_diff_stats_counts_changes(repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    (repo / "a.txt").write_text("a\nb\nc\n")
    (repo / "b.txt").write_text("x\ny\n")
    subprocess.run(["git", "add", "a.txt", "b.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add"], cwd=repo, check=True)

    files, ins, dels = diff_stats(repo, base)
    assert files == 2
    assert ins == 5
    assert dels == 0


def test_diff_stats_no_changes(repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert diff_stats(repo, base) == (0, 0, 0)


# ----- Revision loop integration -------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def stores(
    isolated_home: Path, repo: Path
) -> tuple[RosterStore, ProjectStore, WorktreeManager, Project, Specialist]:
    isolated_home.mkdir(parents=True, exist_ok=True)
    rs = RosterStore()
    ps = ProjectStore()
    wm = WorktreeManager()
    spec = Specialist.from_template("aria", "backend")
    rs.save(spec)
    proj = Project(
        id="abc123def456", name="myapp",
        repo_path=str(repo), assigned_specialists=["aria"],
    )
    ps.save(proj)
    return rs, ps, wm, proj, spec


def _result_msg() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1000, duration_api_ms=900,
        is_error=False, num_turns=2, session_id="s", stop_reason=None,
        total_cost_usd=0.05, usage=None, result=None,
        structured_output=None, model_usage=None, permission_denials=None,
        errors=None, uuid=None,
    )


def _runner_makes_a_commit() -> Any:
    """Stand-in for runner.run_specialist that commits a tiny file."""
    async def fake(**kwargs: Any) -> RunResult:
        cwd = Path(kwargs["cwd"])
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=cwd, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=cwd, check=True)
        f = cwd / f"work-{len(list(cwd.iterdir()))}.txt"
        f.write_text("done\n")
        subprocess.run(["git", "add", str(f.name)], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "feat"], cwd=cwd, check=True)
        result = _result_msg()
        return RunResult(
            status=RunStatus.COMPLETED, final=result, cost_usd=0.05,
            duration_seconds=1.0, turn_count=2,
        )
    return fake


async def _no_memory(**_: Any) -> tuple[Any, float]:
    return None, 0.0


def _make_reviewer(verdicts: list[Review]) -> Any:
    """Returns a fake run_reviewer that returns each verdict in turn."""
    iterator = iter(verdicts)

    async def fake(**_: Any) -> tuple[Review, float]:
        try:
            return next(iterator), 0.02
        except StopIteration:
            raise AssertionError("reviewer called more times than expected") from None
    return fake


def test_review_approves_first_round(
    stores: tuple[RosterStore, ProjectStore, WorktreeManager, Project, Specialist],
) -> None:
    rs, ps, wm, proj, spec = stores
    rev_approve = Review(approved=True, summary="lgtm", issues=[])

    with patch.object(runner_mod, "run_specialist", _runner_makes_a_commit()):
        with patch.object(reviewer, "run_reviewer", _make_reviewer([rev_approve])):
            with patch.object(mission, "extract_memory_delta", _no_memory):
                meta = asyncio.run(dispatch(
                    project=proj, specialist=spec, ticket="t",
                    roster_store=rs, project_store=ps, worktree_manager=wm,
                    review=True, mission_id="m-rev-1",
                ))

    assert meta.status is MissionStatus.COMPLETED
    assert len(meta.reviews) == 1
    assert meta.reviews[0].approved is True
    assert meta.revision_rounds == 0


def test_review_rejects_then_approves_after_revision(
    stores: tuple[RosterStore, ProjectStore, WorktreeManager, Project, Specialist],
) -> None:
    """First review rejects, specialist re-runs, second review approves."""
    rs, ps, wm, proj, spec = stores
    verdicts = [
        Review(approved=False, summary="missing tests", issues=["no test for greet()"]),
        Review(approved=True, summary="now ok", issues=[]),
    ]
    runner_calls: list[int] = []

    async def counting_runner(**kwargs: Any) -> RunResult:
        runner_calls.append(1)
        result: RunResult = await _runner_makes_a_commit()(**kwargs)
        return result

    with patch.object(runner_mod, "run_specialist", counting_runner):
        with patch.object(reviewer, "run_reviewer", _make_reviewer(verdicts)):
            with patch.object(mission, "extract_memory_delta", _no_memory):
                meta = asyncio.run(dispatch(
                    project=proj, specialist=spec, ticket="t",
                    roster_store=rs, project_store=ps, worktree_manager=wm,
                    review=True, mission_id="m-rev-2",
                ))

    assert meta.status is MissionStatus.COMPLETED
    assert len(meta.reviews) == 2
    assert meta.reviews[0].approved is False
    assert meta.reviews[1].approved is True
    assert meta.revision_rounds == 1  # one re-run after rejection
    assert len(runner_calls) == 2


def test_review_loop_exhausted_marks_review_rejected(
    stores: tuple[RosterStore, ProjectStore, WorktreeManager, Project, Specialist],
) -> None:
    """Reviewer keeps rejecting; status becomes REVIEW_REJECTED after cap."""
    rs, ps, wm, proj, spec = stores
    # 1 initial + 2 revisions = 3 reviews, all rejecting.
    verdicts = [
        Review(approved=False, summary=f"still bad #{i}", issues=[f"problem {i}"])
        for i in range(3)
    ]

    with patch.object(runner_mod, "run_specialist", _runner_makes_a_commit()):
        with patch.object(reviewer, "run_reviewer", _make_reviewer(verdicts)):
            with patch.object(mission, "extract_memory_delta", _no_memory):
                meta = asyncio.run(dispatch(
                    project=proj, specialist=spec, ticket="t",
                    roster_store=rs, project_store=ps, worktree_manager=wm,
                    review=True, max_revisions=2, mission_id="m-rev-3",
                ))

    assert meta.status is MissionStatus.REVIEW_REJECTED
    assert len(meta.reviews) == 3
    assert all(not r.approved for r in meta.reviews)
    assert meta.revision_rounds == 2  # capped
    assert meta.error_detail is not None and "rejected after 3" in meta.error_detail


def test_reviewer_error_does_not_fail_mission(
    stores: tuple[RosterStore, ProjectStore, WorktreeManager, Project, Specialist],
) -> None:
    """If the Reviewer itself crashes, the mission keeps the worker's output."""
    rs, ps, wm, proj, spec = stores

    async def crashing_reviewer(**_: Any) -> tuple[Review, float]:
        raise ReviewError("parse failed")

    with patch.object(runner_mod, "run_specialist", _runner_makes_a_commit()):
        with patch.object(reviewer, "run_reviewer", crashing_reviewer):
            with patch.object(mission, "extract_memory_delta", _no_memory):
                meta = asyncio.run(dispatch(
                    project=proj, specialist=spec, ticket="t",
                    roster_store=rs, project_store=ps, worktree_manager=wm,
                    review=True, mission_id="m-rev-4",
                ))

    # Mission completes (worker did the work), but the review record shows
    # the reviewer error.
    assert len(meta.reviews) == 1
    assert meta.reviews[0].approved is False
    assert "reviewer error" in meta.reviews[0].summary
    # Status is REVIEW_REJECTED because the last review was not approved.
    assert meta.status is MissionStatus.REVIEW_REJECTED


def _make_assistant_msg(text: str) -> Any:
    """Build a minimal AssistantMessage with the given text."""
    from claude_agent_sdk import AssistantMessage, TextBlock

    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="claude-sonnet-4-6",
        parent_tool_use_id=None,
        error=None,
        usage=None,
        message_id="m1",
        stop_reason=None,
        session_id="s1",
        uuid=None,
    )


def _make_result_msg(cost: float = 0.02) -> Any:
    return ResultMessage(
        subtype="success", duration_ms=500, duration_api_ms=400,
        is_error=False, num_turns=1, session_id="s", stop_reason=None,
        total_cost_usd=cost, usage=None, result=None,
        structured_output=None, model_usage=None, permission_denials=None,
        errors=None, uuid=None,
    )


def test_run_reviewer_wires_bash_constraint(tmp_path: Path) -> None:
    """run_reviewer must pass can_use_tool and permission_mode='default' to query."""
    captured: dict[str, Any] = {}
    approve_json = '```json\n{"approved": true, "summary": "lgtm", "issues": []}\n```'

    def fake_query(*, prompt: Any, options: Any, **_: Any) -> Any:
        captured["options"] = options

        async def gen() -> Any:
            # consume the async-iterable prompt
            if hasattr(prompt, "__aiter__"):
                async for _ in prompt:
                    pass
            yield _make_assistant_msg(approve_json)
            yield _make_result_msg()

        return gen()

    from workforce import reviewer as reviewer_mod

    with patch.object(reviewer_mod, "query", fake_query):
        review, cost = asyncio.run(
            reviewer_mod.run_reviewer(
                worktree_path=tmp_path,
                base_sha="abc123",
                ticket="review this",
            )
        )

    assert review.approved is True
    opts = captured["options"]
    # Bash constraint must be active.
    assert opts.can_use_tool is not None, "can_use_tool should be wired in"
    assert opts.permission_mode == "default", (
        "permission_mode must be 'default' for can_use_tool to fire"
    )


def test_review_skipped_when_runner_fails(
    stores: tuple[RosterStore, ProjectStore, WorktreeManager, Project, Specialist],
) -> None:
    """If the worker errored, the Reviewer doesn't run."""
    rs, ps, wm, proj, spec = stores

    async def failing_runner(**_: Any) -> RunResult:
        return RunResult(
            status=RunStatus.ERROR, final=None,
            cost_usd=0.05, duration_seconds=1.0, turn_count=1,
            error_detail="boom",
        )
    reviewer_called = False

    async def fake_reviewer(**_: Any) -> tuple[Review, float]:
        nonlocal reviewer_called
        reviewer_called = True
        return Review(approved=True), 0.0

    with patch.object(runner_mod, "run_specialist", failing_runner):
        with patch.object(reviewer, "run_reviewer", fake_reviewer):
            with patch.object(mission, "extract_memory_delta", _no_memory):
                meta = asyncio.run(dispatch(
                    project=proj, specialist=spec, ticket="t",
                    roster_store=rs, project_store=ps, worktree_manager=wm,
                    review=True, mission_id="m-rev-5",
                ))

    assert meta.status is MissionStatus.ERROR
    assert reviewer_called is False
    assert len(meta.reviews) == 0
