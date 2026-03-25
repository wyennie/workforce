"""Parallel mission dispatch.

Orchestrates: Manager (planning) → Decomposition → fan-out across N
specialists in parallel worktrees → aggregate → merge plan.

Sub-mission ID format: `<parent-id>__<task-id>`. They live alongside the
parent in `missions/`, flat — _find_mission resolves them with the same
code path as any other mission.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from workforce import manager, mission, paths
from workforce.manager import (
    CONTRACT_TASK_ID,
    Decomposition,
    DecompositionKind,
    ManagerError,
    Task,
    ValidationError,
)
from workforce.mission import (
    MissionMeta,
    MissionStatus,
    generate_mission_id,
    mission_paths,
)
from workforce.project import Project, ProjectStore
from workforce.runner import EventCallback, RunLimits
from workforce.specialist import RosterStore, Specialist
from workforce.worktree import WorktreeManager


SCHEMA_VERSION = 1


# ----- Models ---------------------------------------------------------------


class ParallelStatus(str, Enum):
    PLANNED = "planned"            # Manager done, sub-missions not yet run
    DISPATCHED = "dispatched"      # All sub-missions started
    COMPLETED = "completed"        # All sub-missions completed cleanly
    PARTIAL = "partial"            # Mix of completed + failed/timeout
    FAILED = "failed"              # All sub-missions failed
    CANCELLED = "cancelled"        # User declined the decomposition


class SubMissionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    mission_id: str
    specialist: str
    status: MissionStatus | None = None  # None = not yet run


class ParallelMissionMeta(BaseModel):
    """Saved as `meta.json` in the parent mission directory."""
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    parent_mission_id: str
    project_id: str
    project_name: str
    ticket: str
    started_at: str
    ended_at: str | None = None
    manager_cost_usd: float = 0.0
    sub_missions: list[SubMissionRef] = Field(default_factory=list)
    status: ParallelStatus
    decomposition_kind: DecompositionKind
    merge_order: list[str] = Field(default_factory=list)


# ----- Specialist resolution ------------------------------------------------


@dataclass
class _ResolvedTask:
    task: Task
    specialist: Specialist
    sub_mission_id: str


class ResolutionError(Exception):
    """Couldn't pin a specialist to a task."""


def resolve_task_specialists(
    decomp: Decomposition,
    *,
    parent_mission_id: str,
    project: Project,
    roster_store: RosterStore,
    fallback_specialist: str | None = None,
) -> list[_ResolvedTask]:
    """Pick a Specialist for each task. Suggested name wins if assigned;
    else fall back to the explicit fallback; else error.
    """
    assigned = set(project.assigned_specialists)
    resolved: list[_ResolvedTask] = []
    for task in decomp.tasks:
        choice = task.suggested_specialist
        if choice and choice not in assigned:
            choice = None
        if choice is None and fallback_specialist:
            choice = fallback_specialist
        if choice is None and len(assigned) == 1:
            choice = next(iter(assigned))
        if choice is None:
            raise ResolutionError(
                f"task {task.id!r} suggests {task.suggested_specialist!r} which "
                f"isn't assigned to {project.name!r} (have: "
                f"{', '.join(sorted(assigned)) or 'none'}). Pass a fallback."
            )
        if not roster_store.exists(choice):
            raise ResolutionError(
                f"task {task.id!r} resolved to {choice!r} but no such "
                "specialist in the roster"
            )
        spec = roster_store.load(choice)
        sub_id = f"{parent_mission_id}__{task.id}"
        resolved.append(_ResolvedTask(task=task, specialist=spec, sub_mission_id=sub_id))
    return resolved


# ----- Orchestration --------------------------------------------------------


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _materialize_contract(decomp: Decomposition, parent_dir: Path) -> Path | None:
    """Write the contract to the parent mission directory; return its path."""
    if not decomp.contract.needed or not decomp.contract.body.strip():
        return None
    contract_dir = parent_dir / "contract"
    contract_dir.mkdir(parents=True, exist_ok=True)
    out = contract_dir / "contract.md"
    out.write_text(decomp.contract.body.rstrip() + "\n")
    return out


def _aggregate_status(sub_metas: list[MissionMeta]) -> ParallelStatus:
    if not sub_metas:
        return ParallelStatus.FAILED
    all_done = all(m.status is MissionStatus.COMPLETED for m in sub_metas)
    any_done = any(m.status is MissionStatus.COMPLETED for m in sub_metas)
    if all_done:
        return ParallelStatus.COMPLETED
    if any_done:
        return ParallelStatus.PARTIAL
    return ParallelStatus.FAILED


@dataclass
class ParallelDispatchResult:
    parent_meta: ParallelMissionMeta
    decomposition: Decomposition
    sub_metas: list[MissionMeta]
    contract_path: Path | None = None


async def dispatch_parallel(
    *,
    project: Project,
    ticket: str,
    roster_store: RosterStore,
    project_store: ProjectStore,
    worktree_manager: WorktreeManager,
    sub_mission_limits: RunLimits | None = None,
    on_manager_message: EventCallback | None = None,
    make_sub_callback: "SubCallbackFactory | None" = None,
    fallback_specialist: str | None = None,
    confirm: "ConfirmCallback | None" = None,
    parent_mission_id: str | None = None,
    decomposition_override: Decomposition | None = None,
) -> ParallelDispatchResult:
    """Plan with the Manager, validate, optionally confirm, then fan out.

    `decomposition_override` skips the Manager entirely (used for tests and
    for `--decomposition <file>` in a future CLI flag). `confirm` is invoked
    after validation with the decomp; it returns True to proceed, False to
    cancel.
    """
    parent_mission_id = parent_mission_id or generate_mission_id()
    parent_paths = mission_paths(project.id, parent_mission_id)
    parent_paths.root.mkdir(parents=True, exist_ok=True)

    started_iso = _now_iso()
    manager_cost = 0.0

    # ---- 1. Plan (or use override) ----
    if decomposition_override is not None:
        decomp = decomposition_override
    else:
        try:
            decomp, manager_cost, _ = await manager.run_manager(
                ticket=ticket,
                repo_path=Path(project.repo_path),
                available_specialists=list(project.assigned_specialists),
            )
        except ManagerError as e:
            _save_parent_meta(
                parent_paths.meta,
                ParallelMissionMeta(
                    parent_mission_id=parent_mission_id,
                    project_id=project.id,
                    project_name=project.name,
                    ticket=ticket,
                    started_at=started_iso,
                    ended_at=_now_iso(),
                    manager_cost_usd=manager_cost,
                    status=ParallelStatus.FAILED,
                    decomposition_kind=DecompositionKind.SINGLE,
                ),
            )
            raise ManagerError(f"manager failed: {e}") from e

    # Persist decomposition.json now so the user has a record even if
    # validation or dispatch fails next.
    (parent_paths.root / "decomposition.json").write_text(
        decomp.model_dump_json(indent=2) + "\n"
    )

    # ---- 2. Validate ----
    manager.validate_decomposition(
        decomp,
        repo_path=Path(project.repo_path),
        available_specialists=list(project.assigned_specialists) or None,
    )

    # ---- 3. Resolve specialists ----
    resolved = resolve_task_specialists(
        decomp,
        parent_mission_id=parent_mission_id,
        project=project,
        roster_store=roster_store,
        fallback_specialist=fallback_specialist,
    )

    # ---- 4. Confirm ----
    if confirm is not None:
        if not confirm(decomp, [(r.task.id, r.specialist.name) for r in resolved]):
            parent_meta = ParallelMissionMeta(
                parent_mission_id=parent_mission_id,
                project_id=project.id,
                project_name=project.name,
                ticket=ticket,
                started_at=started_iso,
                ended_at=_now_iso(),
                manager_cost_usd=manager_cost,
                status=ParallelStatus.CANCELLED,
                decomposition_kind=decomp.kind,
                merge_order=list(decomp.merge_order),
            )
            _save_parent_meta(parent_paths.meta, parent_meta)
            return ParallelDispatchResult(
                parent_meta=parent_meta,
                decomposition=decomp,
                sub_metas=[],
            )

    # ---- 5. Materialize contract artifact ----
    contract_path = _materialize_contract(decomp, parent_paths.root)
    contract_text = decomp.contract.body if decomp.contract.needed else None

    # ---- 6. Fan out ----
    sub_refs = [
        SubMissionRef(
            task_id=r.task.id,
            mission_id=r.sub_mission_id,
            specialist=r.specialist.name,
        )
        for r in resolved
    ]
    parent_meta = ParallelMissionMeta(
        parent_mission_id=parent_mission_id,
        project_id=project.id,
        project_name=project.name,
        ticket=ticket,
        started_at=started_iso,
        ended_at=None,
        manager_cost_usd=manager_cost,
        sub_missions=sub_refs,
        status=ParallelStatus.DISPATCHED,
        decomposition_kind=decomp.kind,
        merge_order=list(decomp.merge_order),
    )
    _save_parent_meta(parent_paths.meta, parent_meta)

    sub_metas = await _run_sub_missions(
        resolved=resolved,
        project=project,
        roster_store=roster_store,
        project_store=project_store,
        worktree_manager=worktree_manager,
        contract_text=contract_text,
        limits=sub_mission_limits,
        make_sub_callback=make_sub_callback,
    )

    # ---- 7. Final parent meta ----
    sub_status_by_id = {m.mission_id: m.status for m in sub_metas}
    for ref in sub_refs:
        ref.status = sub_status_by_id.get(ref.mission_id)
    parent_meta.sub_missions = sub_refs
    parent_meta.status = _aggregate_status(sub_metas)
    parent_meta.ended_at = _now_iso()
    _save_parent_meta(parent_paths.meta, parent_meta)

    return ParallelDispatchResult(
        parent_meta=parent_meta,
        decomposition=decomp,
        sub_metas=sub_metas,
        contract_path=contract_path,
    )


async def _run_sub_missions(
    *,
    resolved: list[_ResolvedTask],
    project: Project,
    roster_store: RosterStore,
    project_store: ProjectStore,
    worktree_manager: WorktreeManager,
    contract_text: str | None,
    limits: RunLimits | None,
    make_sub_callback: "SubCallbackFactory | None",
) -> list[MissionMeta]:
    """Run all sub-missions concurrently and gather results."""

    async def one(r: _ResolvedTask) -> MissionMeta:
        # Each sub-mission gets the contract injected as extra_context.
        extra = (
            f"## Contract\n\n{contract_text.strip()}"
            if contract_text
            else None
        )
        cb = make_sub_callback(r.task.id) if make_sub_callback is not None else None
        return await mission.dispatch(
            project=project,
            specialist=r.specialist,
            ticket=r.task.description,
            roster_store=roster_store,
            project_store=project_store,
            worktree_manager=worktree_manager,
            limits=limits,
            on_message=cb,
            mission_id=r.sub_mission_id,
            extra_context=extra,
        )

    return await asyncio.gather(*(one(r) for r in resolved))


def _save_parent_meta(path: Path, meta: ParallelMissionMeta) -> None:
    path.write_text(meta.model_dump_json(indent=2) + "\n")


# ----- Callback signatures (for type-only purposes) -------------------------

from collections.abc import Callable

ConfirmCallback = Callable[[Decomposition, list[tuple[str, str]]], bool]
SubCallbackFactory = Callable[[str], EventCallback]


# ----- Merge plan ----------------------------------------------------------


@dataclass
class MergeStep:
    task_id: str
    branch: str
    sub_mission_id: str
    status: MissionStatus | None


def merge_plan(
    parent_meta: ParallelMissionMeta,
    sub_metas: list[MissionMeta],
) -> list[MergeStep]:
    """Build a merge plan in `parent_meta.merge_order`. Skips failed subs."""
    by_id = {m.mission_id: m for m in sub_metas}
    by_task = {ref.task_id: ref for ref in parent_meta.sub_missions}
    plan: list[MergeStep] = []
    order = parent_meta.merge_order or [r.task_id for r in parent_meta.sub_missions]
    for task_id in order:
        ref = by_task.get(task_id)
        if ref is None:
            continue
        sub = by_id.get(ref.mission_id)
        if sub is None:
            continue
        plan.append(
            MergeStep(
                task_id=task_id,
                branch=sub.branch,
                sub_mission_id=ref.mission_id,
                status=sub.status,
            )
        )
    return plan
