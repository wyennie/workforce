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
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from workforce import manager, mission
from workforce.manager import (
    CONTRACT_TASK_ID,
    Decomposition,
    DecompositionKind,
    ManagerError,
    SpecialistInfo,
    Task,
)
from workforce.mission import (
    MissionMeta,
    MissionStatus,
    generate_mission_id,
    mission_paths,
)
from workforce.project import Project, ProjectStore
from workforce.runner import EventCallback, RunLimits
from workforce.specialist import (
    TEMPLATES,
    RosterError,
    RosterStore,
    Specialist,
)
from workforce.worktree import WorktreeManager

SCHEMA_VERSION = 1


# ----- Models ---------------------------------------------------------------


class ParallelStatus(StrEnum):
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
    out_of_lane_files: list[str] = Field(default_factory=list)


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
    staffing_action: str = "already_assigned"
    # one of: already_assigned | auto_assigned_from_roster | auto_hired_from_template | fallback


class ResolutionError(Exception):
    """Couldn't pin a specialist to a task."""


def resolve_task_specialists(
    decomp: Decomposition,
    *,
    parent_mission_id: str,
    project: Project,
    roster_store: RosterStore,
    project_store: ProjectStore,
    fallback_specialist: str | None = None,
    auto_staff: bool = True,
) -> list[_ResolvedTask]:
    """Pick a Specialist for each task. Resolution priority:

    1. `suggested_specialist` is assigned to the project → use them.
    2. `suggested_specialist` exists in the global roster → if `auto_staff`,
       assign them to the project; else fall through.
    3. `suggested_specialist` doesn't exist + `template_hint` set + `auto_staff`
       → hire from template, assign to project, use.
    4. `fallback_specialist` set → use them.
    5. Project has exactly one assigned specialist → use them.
    6. Error.

    Mutates `project.assigned_specialists` and persists via `project_store`
    when auto-assigning or auto-hiring.
    """
    resolved: list[_ResolvedTask] = []
    project_dirty = False

    for task in decomp.tasks:
        spec, action, dirty = _staff_one_task(
            task=task,
            project=project,
            roster_store=roster_store,
            fallback_specialist=fallback_specialist,
            auto_staff=auto_staff,
        )
        if dirty:
            project_dirty = True
        sub_id = f"{parent_mission_id}__{task.id}"
        resolved.append(
            _ResolvedTask(
                task=task,
                specialist=spec,
                sub_mission_id=sub_id,
                staffing_action=action,
            )
        )

    if project_dirty:
        project_store.save(project, overwrite=True)

    return resolved


def _staff_one_task(
    *,
    task: Task,
    project: Project,
    roster_store: RosterStore,
    fallback_specialist: str | None,
    auto_staff: bool,
) -> tuple[Specialist, str, bool]:
    """Resolve one task to a Specialist; return (spec, action, project_dirty)."""
    assigned = set(project.assigned_specialists)
    name = task.suggested_specialist

    # 1. Already assigned.
    if name and name in assigned:
        return roster_store.load(name), "already_assigned", False

    # 2. In roster but not assigned — auto-assign if allowed.
    if name and roster_store.exists(name):
        if auto_staff:
            project.assigned_specialists.append(name)
            return roster_store.load(name), "auto_assigned_from_roster", True
        # else fall through to fallback

    # 3. Doesn't exist + template_hint provided + auto-staff allowed → hire.
    if (
        name
        and not roster_store.exists(name)
        and task.template_hint
        and auto_staff
    ):
        if task.template_hint not in TEMPLATES:
            raise ResolutionError(
                f"task {task.id!r} requested template {task.template_hint!r} "
                f"which doesn't exist; available: {', '.join(sorted(TEMPLATES))}"
            )
        try:
            spec = Specialist.from_template(name, task.template_hint)
            roster_store.save(spec)
        except (RosterError, ValueError) as e:
            raise ResolutionError(
                f"task {task.id!r}: failed to auto-hire {name!r} from template "
                f"{task.template_hint!r}: {e}"
            ) from e
        project.assigned_specialists.append(name)
        return spec, "auto_hired_from_template", True

    # 4. Fallback specialist.
    if fallback_specialist:
        if not roster_store.exists(fallback_specialist):
            raise ResolutionError(
                f"fallback specialist {fallback_specialist!r} doesn't exist"
            )
        if fallback_specialist not in assigned and auto_staff:
            project.assigned_specialists.append(fallback_specialist)
            return roster_store.load(fallback_specialist), "fallback", True
        return roster_store.load(fallback_specialist), "fallback", False

    # 5. One assigned specialist on the project — use them.
    if len(project.assigned_specialists) == 1:
        only = project.assigned_specialists[0]
        return roster_store.load(only), "fallback", False

    # 6. Out of options.
    if name and not roster_store.exists(name) and not task.template_hint:
        # Common case: Manager suggested a new name but didn't tell us how
        # to hire from a template.
        raise ResolutionError(
            f"task {task.id!r}: Manager suggested specialist {name!r} which "
            "doesn't exist, and didn't provide a `template_hint`. Either:\n"
            f"  - re-run dispatch (Manager may pick a different name), or\n"
            f"  - hire it manually first: workforce hire {name} --from-template <backend|frontend|tester|reviewer|generalist>\n"
            f"  - assign an existing specialist to the project"
        )
    raise ResolutionError(
        f"task {task.id!r}: cannot resolve specialist (suggested={name!r}, "
        f"template_hint={task.template_hint!r}, assigned to project: "
        f"{', '.join(sorted(assigned)) or 'none'}). "
        f"Either assign specialists to the project or re-run dispatch."
    )


# ----- Orchestration --------------------------------------------------------


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_specialist_info(
    project: Project,
    roster_store: RosterStore,
    project_store: ProjectStore,
) -> list[SpecialistInfo]:
    """Compact view of project-assigned specialists with mission counts.

    Mission count = how many missions on THIS project have a meta.json
    naming this specialist with status=completed. Cheap scan; project
    histories are small.
    """
    missions_dir = project_store.missions_dir(project.id)
    counts: dict[str, int] = {}
    if missions_dir.is_dir():
        for d in missions_dir.iterdir():
            if not d.is_dir():
                continue
            meta_path = d / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                import json as _json
                meta = _json.loads(meta_path.read_text())
            except (OSError, ValueError):
                continue
            if meta.get("status") != "completed":
                continue
            name = meta.get("specialist")
            if isinstance(name, str):
                counts[name] = counts.get(name, 0) + 1

    out: list[SpecialistInfo] = []
    for name in project.assigned_specialists:
        if not roster_store.exists(name):
            continue
        spec = roster_store.load(name)
        out.append(
            SpecialistInfo(
                name=name,
                role=spec.role,
                project_missions=counts.get(name, 0),
            )
        )
    return out


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
    make_sub_callback: SubCallbackFactory | None = None,
    fallback_specialist: str | None = None,
    confirm: ConfirmCallback | None = None,
    parent_mission_id: str | None = None,
    decomposition_override: Decomposition | None = None,
    auto_staff: bool = True,
    review: bool = False,
    max_revisions: int = 3,
    base_branch: str | None = None,
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
        specs_info = _build_specialist_info(project, roster_store, project_store)
        try:
            decomp, manager_cost, _ = await manager.run_manager(
                ticket=ticket,
                repo_path=Path(project.repo_path),
                project_specialists=specs_info,
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

    # ---- 3. Resolve specialists (auto-assign + auto-hire if allowed) ----
    resolved = resolve_task_specialists(
        decomp,
        parent_mission_id=parent_mission_id,
        project=project,
        roster_store=roster_store,
        project_store=project_store,
        fallback_specialist=fallback_specialist,
        auto_staff=auto_staff,
    )

    # ---- 4. Confirm ----
    if confirm is not None:
        confirm_rows = [
            (r.task.id, r.specialist.name, r.staffing_action) for r in resolved
        ]
        if not confirm(decomp, confirm_rows):
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
        review=review,
        max_revisions=max_revisions,
        base_branch=base_branch,
    )

    # ---- 7. Final parent meta ----
    sub_status_by_id = {m.mission_id: m.status for m in sub_metas}
    sub_meta_by_id = {m.mission_id: m for m in sub_metas}
    resolved_by_sub_id = {r.sub_mission_id: r for r in resolved}
    for ref in sub_refs:
        ref.status = sub_status_by_id.get(ref.mission_id)
        # Audit ownership for completed sub-missions only.
        sm = sub_meta_by_id.get(ref.mission_id)
        rt = resolved_by_sub_id.get(ref.mission_id)
        if (
            sm is not None
            and rt is not None
            and sm.status is MissionStatus.COMPLETED
            and sm.worktree_path is not None
            and sm.base_sha is not None
        ):
            try:
                ref.out_of_lane_files = manager.audit_ownership(
                    Path(sm.worktree_path),
                    sm.base_sha,
                    rt.task.owns_paths,
                    rt.task.excludes_paths,
                )
            except Exception:
                # Audit failure shouldn't crash dispatch; just leave it empty.
                ref.out_of_lane_files = []
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


def _topological_waves(tasks: list[Task]) -> list[list[Task]]:
    """Group tasks into waves that can run in parallel respecting depends_on.

    Wave 0 = tasks with no real dependencies (the synthetic 'contract' is
    always satisfied).
    Wave N+1 = tasks whose deps are all in waves 0..N.

    Used for sequential execution: each wave runs in parallel via
    asyncio.gather, but later waves wait for earlier ones to complete so
    they can fork from the right branch tips.
    """
    waves: list[list[Task]] = []
    remaining = {t.id: t for t in tasks}
    completed: set[str] = set()
    while remaining:
        ready = [
            t for t in remaining.values()
            if all(d == CONTRACT_TASK_ID or d in completed for d in t.depends_on)
        ]
        if not ready:
            raise ValueError(
                "dependency cycle in remaining tasks: " + ", ".join(remaining)
            )
        waves.append(ready)
        for t in ready:
            del remaining[t.id]
            completed.add(t.id)
    return waves


def _skipped_meta(
    *,
    project: Project,
    specialist_name: str,
    sub_mission_id: str,
    ticket: str,
    failed_deps: list[str],
) -> MissionMeta:
    """Synthetic MissionMeta for a sub-mission whose deps failed."""
    now = _now_iso()
    return MissionMeta(
        mission_id=sub_mission_id,
        project_id=project.id,
        project_name=project.name,
        specialist=specialist_name,
        model="(skipped)",
        ticket=ticket,
        branch="(none)",
        worktree_path="(none)",
        base_sha="(none)",
        started_at=now,
        ended_at=now,
        duration_seconds=0.0,
        status=MissionStatus.ERROR,
        error_detail=f"skipped — dependency task(s) failed: {', '.join(failed_deps)}",
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
    make_sub_callback: SubCallbackFactory | None,
    review: bool = False,
    max_revisions: int = 3,
    base_branch: str | None = None,
) -> list[MissionMeta]:
    """Run sub-missions wave-by-wave, honoring depends_on for sequential cases."""
    extra_context = (
        f"## Contract\n\n{contract_text.strip()}" if contract_text else None
    )

    by_task_id = {r.task.id: r for r in resolved}
    waves = _topological_waves([r.task for r in resolved])

    # Track which tasks completed cleanly. For repo missions we also remember
    # the branch tip so dependent waves can fork from it; workspace missions
    # don't have branches (all sub-missions share the workspace cwd) so the
    # branch map stays empty.
    completed_tasks: set[str] = set()
    completed_branches: dict[str, str] = {}
    all_metas: list[MissionMeta] = []

    async def run_one(
        r: _ResolvedTask,
        start_point: str | None,
        additional_merges: list[str],
    ) -> MissionMeta:
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
            extra_context=extra_context,
            start_point=start_point,
            additional_merges=additional_merges or None,
            review=review,
            max_revisions=max_revisions,
            contract=contract_text,
            owns_paths=r.task.owns_paths,
            excludes_paths=r.task.excludes_paths,
        )

    for wave in waves:
        wave_resolved = [by_task_id[t.id] for t in wave]
        coros = []
        skips: list[MissionMeta] = []

        for r in wave_resolved:
            real_deps = [d for d in r.task.depends_on if d != CONTRACT_TASK_ID]
            failed_deps = [d for d in real_deps if d not in completed_tasks]
            if failed_deps:
                skips.append(_skipped_meta(
                    project=project,
                    specialist_name=r.specialist.name,
                    sub_mission_id=r.sub_mission_id,
                    ticket=r.task.description,
                    failed_deps=failed_deps,
                ))
                continue

            # Workspace projects don't fork branches; all sub-missions run in
            # the project dir directly. Repo projects fork from each dep's
            # branch tip and merge any siblings in before starting; root tasks
            # fork from `base_branch` if set, else current HEAD.
            if not real_deps or project.kind == "workspace":
                start_point = None if project.kind == "workspace" else base_branch
                additional_merges: list[str] = []
            else:
                primary = real_deps[0]
                start_point = completed_branches[primary]
                additional_merges = [completed_branches[d] for d in real_deps[1:]]

            coros.append(run_one(r, start_point, additional_merges))

        # Save synthetic skip metas now so they show up in mission show.
        for meta in skips:
            mp = mission.mission_paths(project.id, meta.mission_id)
            mp.root.mkdir(parents=True, exist_ok=True)
            mp.meta.write_text(meta.model_dump_json(indent=2) + "\n")
            all_metas.append(meta)

        # Run runnable tasks in this wave concurrently.
        if coros:
            wave_metas = await asyncio.gather(*coros)
            for meta in wave_metas:
                all_metas.append(meta)
                if meta.status is MissionStatus.COMPLETED:
                    task_id = meta.mission_id.rsplit("__", 1)[-1]
                    completed_tasks.add(task_id)
                    if meta.branch is not None:
                        # Repo missions only — workspace missions have no branch.
                        completed_branches[task_id] = meta.branch

    return all_metas


def _save_parent_meta(path: Path, meta: ParallelMissionMeta) -> None:
    path.write_text(meta.model_dump_json(indent=2) + "\n")


# ----- Callback signatures (for type-only purposes) -------------------------

# confirm receives (decomposition, [(task_id, specialist_name, staffing_action)])
ConfirmCallback = Callable[[Decomposition, list[tuple[str, str, str]]], bool]
SubCallbackFactory = Callable[[str], EventCallback]


# ----- Merge plan ----------------------------------------------------------


@dataclass
class MergeStep:
    task_id: str
    branch: str
    sub_mission_id: str
    status: MissionStatus | None


@dataclass
class AutoMergeStepResult:
    task_id: str
    branch: str
    success: bool
    detail: str   # "merged" / "conflict" / "skipped" / "aborted" / "error: ..."
    conflicting_files: list[str] = field(default_factory=list)


class MergePreflightError(Exception):
    """Source repo isn't in a state we can safely auto-merge into."""


def _branch_exists(repo: Path, branch: str) -> bool:
    r = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo, capture_output=True, text=True,
    )
    return r.returncode == 0


def _current_branch(repo: Path) -> str | None:
    r = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=repo, capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def _is_clean(repo: Path) -> bool:
    """True iff `git status --porcelain` has no staged/modified entries
    (untracked files are tolerated, matching worktree manager policy)."""
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout
    return all(line.startswith("??") for line in out.splitlines() if line)


def auto_merge_into(
    repo_path: Path,
    plan: list[MergeStep],
    *,
    target_branch: str,
) -> list[AutoMergeStepResult]:
    """Switch to `target_branch` if needed, then run the merge plan against it.

    Preflight: the target must exist and the source must be clean (no
    staged/modified changes). On any failure leaves the source on the
    target branch (does not switch back to where the user started — they
    asked for the merge to land here).
    """
    if not _branch_exists(repo_path, target_branch):
        raise MergePreflightError(
            f"target branch {target_branch!r} doesn't exist in {repo_path}"
        )
    if not _is_clean(repo_path):
        raise MergePreflightError(
            f"{repo_path} has uncommitted changes; commit or stash before "
            "auto-merging (untracked files are fine)"
        )
    current = _current_branch(repo_path)
    if current != target_branch:
        switch = subprocess.run(
            ["git", "switch", target_branch],
            cwd=repo_path, capture_output=True, text=True, check=False,
        )
        if switch.returncode != 0:
            raise MergePreflightError(
                f"could not switch to {target_branch!r}: "
                + (switch.stderr.strip() or switch.stdout.strip())
            )
    return auto_merge(repo_path, plan)


def auto_merge(repo_path: Path, plan: list[MergeStep]) -> list[AutoMergeStepResult]:
    """Run `git merge --no-ff <branch>` for each step in order against the source repo.

    Merges into whatever branch is currently checked out — does NOT switch
    branches. Use `auto_merge_into` for explicit-target behavior.

    On the first non-zero exit, aborts the in-progress merge and marks remaining
    steps as skipped. Steps whose source mission didn't complete cleanly are
    skipped without running git.

    Returns one AutoMergeStepResult per input step.
    """
    results: list[AutoMergeStepResult] = []
    aborted = False
    for step in plan:
        if aborted:
            results.append(AutoMergeStepResult(
                task_id=step.task_id, branch=step.branch,
                success=False, detail="skipped (earlier step failed)",
            ))
            continue
        if step.status is not None and step.status is not MissionStatus.COMPLETED:
            results.append(AutoMergeStepResult(
                task_id=step.task_id, branch=step.branch,
                success=False, detail=f"skipped (sub-mission status: {step.status.value})",
            ))
            continue
        try:
            r = subprocess.run(
                ["git", "merge", "--no-ff", step.branch],
                cwd=repo_path, capture_output=True, text=True, check=False,
            )
        except OSError as e:
            results.append(AutoMergeStepResult(
                task_id=step.task_id, branch=step.branch,
                success=False, detail=f"git invoke failed: {e}",
            ))
            aborted = True
            continue
        if r.returncode == 0:
            results.append(AutoMergeStepResult(
                task_id=step.task_id, branch=step.branch,
                success=True, detail="merged",
            ))
        else:
            # Capture conflicting files BEFORE aborting (the abort wipes them
            # from the working tree). `--diff-filter=U` lists unmerged paths.
            conflicting: list[str] = []
            try:
                conflict_out = subprocess.run(
                    ["git", "diff", "--name-only", "--diff-filter=U"],
                    cwd=repo_path, capture_output=True, text=True, check=False,
                )
                if conflict_out.returncode == 0:
                    conflicting = [
                        line.strip()
                        for line in conflict_out.stdout.splitlines()
                        if line.strip()
                    ]
            except OSError:
                pass

            # Best-effort abort so the source repo isn't left mid-merge
            subprocess.run(
                ["git", "merge", "--abort"],
                cwd=repo_path, capture_output=True, text=True, check=False,
            )
            err = (r.stderr.strip() or r.stdout.strip())[:200]
            results.append(AutoMergeStepResult(
                task_id=step.task_id, branch=step.branch,
                success=False, detail=f"conflict or merge error: {err}",
                conflicting_files=conflicting,
            ))
            aborted = True
    return results


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
        if sub is None or sub.branch is None:
            # Workspace sub-missions have no branch; merge plans don't apply.
            # All edits already landed in the shared workspace dir.
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
