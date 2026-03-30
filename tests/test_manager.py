"""Tests for workforce.manager: model, parser, validator."""

from __future__ import annotations

from pathlib import Path

import pytest

from workforce.manager import (
    Contract,
    Decomposition,
    DecompositionKind,
    ManagerError,
    Task,
    ValidationError,
    audit_ownership,
    parse_decomposition,
    validate_decomposition,
)

# ----- Models ----------------------------------------------------------------


def test_task_id_validation() -> None:
    Task(id="impl", description="x")  # ok
    with pytest.raises(ValueError):
        Task(id="Impl", description="x")
    with pytest.raises(ValueError):
        Task(id="impl with space", description="x")
    with pytest.raises(ValueError):
        Task(id="123", description="x")
    with pytest.raises(ValueError):
        Task(id="x" * 33, description="x")


def test_decomposition_construct() -> None:
    d = Decomposition(
        ticket="t",
        kind=DecompositionKind.SINGLE,
        rationale="r",
        tasks=[Task(id="solo", description="do everything")],
        merge_order=["solo"],
    )
    assert d.kind is DecompositionKind.SINGLE


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValueError):
        Decomposition.model_validate(
            {
                "ticket": "t",
                "kind": "single",
                "rationale": "r",
                "tasks": [],
                "wat": True,
            }
        )


# ----- parse_decomposition --------------------------------------------------


def _decomp_json(**overrides: object) -> str:
    body = {
        "schema_version": 1,
        "ticket": "t",
        "kind": "single",
        "rationale": "r",
        "contract": {"needed": False, "path": "", "body": ""},
        "tasks": [{"id": "solo", "description": "do it", "depends_on": [], "estimated_turns": 5}],
        "merge_order": ["solo"],
    }
    body.update(overrides)
    import json
    return json.dumps(body)


def test_parse_decomposition_fenced() -> None:
    text = "Sure thing:\n```json\n" + _decomp_json() + "\n```\nThanks."
    d = parse_decomposition(text)
    assert d.kind is DecompositionKind.SINGLE


def test_parse_decomposition_unfenced() -> None:
    d = parse_decomposition(_decomp_json())
    assert d.ticket == "t"


def test_parse_decomposition_uses_last_when_multiple() -> None:
    early = _decomp_json(ticket="early")
    late = _decomp_json(ticket="late")
    text = f"first try:\n```json\n{early}\n```\nactually:\n```json\n{late}\n```"
    d = parse_decomposition(text)
    assert d.ticket == "late"


def test_parse_decomposition_garbage_raises() -> None:
    with pytest.raises(ManagerError):
        parse_decomposition("not json at all")
    with pytest.raises(ManagerError):
        parse_decomposition("```json\n{not valid\n```")


def test_parse_decomposition_invalid_schema_raises() -> None:
    text = '```json\n{"kind": "wat"}\n```'
    with pytest.raises(ManagerError):
        parse_decomposition(text)


# ----- validate_decomposition -----------------------------------------------


def _build(
    *tasks: Task,
    kind: DecompositionKind = DecompositionKind.PARALLEL,
    merge_order: list[str] | None = None,
    contract_needed: bool = False,
) -> Decomposition:
    if merge_order is None:
        merge_order = [t.id for t in tasks]
    return Decomposition(
        ticket="t",
        kind=kind,
        rationale="r",
        contract=Contract(needed=contract_needed, path="c.md" if contract_needed else "", body="..." if contract_needed else ""),
        tasks=list(tasks),
        merge_order=merge_order,
    )


def test_validate_no_tasks_raises() -> None:
    d = _build(kind=DecompositionKind.SINGLE, merge_order=[])
    with pytest.raises(ValidationError, match="no tasks"):
        validate_decomposition(d)


def test_validate_duplicate_ids() -> None:
    d = _build(
        Task(id="a", description="x"),
        Task(id="a", description="y"),
    )
    with pytest.raises(ValidationError, match="duplicate"):
        validate_decomposition(d)


def test_validate_unknown_dependency() -> None:
    d = _build(
        Task(id="impl", description="x", depends_on=["ghost"]),
    )
    with pytest.raises(ValidationError, match="unknown task"):
        validate_decomposition(d)


def test_validate_contract_pseudo_dependency_ok() -> None:
    d = _build(
        Task(id="impl", description="x", depends_on=["contract"]),
        Task(id="tests", description="y", depends_on=["contract"]),
        contract_needed=True,
    )
    validate_decomposition(d)  # should not raise


def test_validate_dependency_cycle() -> None:
    d = _build(
        Task(id="a", description="x", depends_on=["b"]),
        Task(id="b", description="y", depends_on=["a"]),
        merge_order=["a", "b"],
    )
    with pytest.raises(ValidationError, match="cycle"):
        validate_decomposition(d)


def test_validate_merge_order_missing_task() -> None:
    d = _build(
        Task(id="a", description="x"),
        Task(id="b", description="y"),
        merge_order=["a"],  # missing b
    )
    with pytest.raises(ValidationError, match="merge_order"):
        validate_decomposition(d)


def test_validate_merge_order_violates_dependency() -> None:
    d = _build(
        Task(id="a", description="x", depends_on=["b"]),
        Task(id="b", description="y"),
        merge_order=["a", "b"],  # 'a' depends on 'b' but is merged first
    )
    with pytest.raises(ValidationError, match="merge_order violates"):
        validate_decomposition(d)


def test_validate_does_not_enforce_specialist_existence() -> None:
    """Specialist resolution is the resolver's job (it can auto-staff).

    Validator must not reject decompositions just because the Manager
    suggested a name not yet in the project's roster.
    """
    d = _build(
        Task(id="a", description="x", suggested_specialist="ghost"),
    )
    validate_decomposition(d, available_specialists=["aria", "ben"])  # no raise


# ----- Path overlap (real filesystem) ---------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A fake repo tree with a few files so glob patterns resolve."""
    (tmp_path / "src" / "auth").mkdir(parents=True)
    (tmp_path / "src" / "auth" / "session.py").write_text("")
    (tmp_path / "src" / "auth" / "__init__.py").write_text("")
    (tmp_path / "src" / "api" / "routes").mkdir(parents=True)
    (tmp_path / "src" / "api" / "routes" / "users.py").write_text("")
    (tmp_path / "tests" / "auth").mkdir(parents=True)
    (tmp_path / "tests" / "auth" / "test_session.py").write_text("")
    (tmp_path / "README.md").write_text("")
    return tmp_path


def test_validate_no_overlap(repo: Path) -> None:
    d = _build(
        Task(id="impl", description="x", owns_paths=["src/auth/**"], depends_on=["contract"]),
        Task(id="tests", description="y", owns_paths=["tests/auth/**"], depends_on=["contract"]),
        contract_needed=True,
    )
    validate_decomposition(d, repo_path=repo)  # no raise


def test_validate_overlap_raises(repo: Path) -> None:
    d = _build(
        Task(id="a", description="x", owns_paths=["src/**"], depends_on=["contract"]),
        Task(id="b", description="y", owns_paths=["src/auth/**"], depends_on=["contract"]),
        contract_needed=True,
    )
    with pytest.raises(ValidationError, match="both claim files"):
        validate_decomposition(d, repo_path=repo)


def test_validate_overlap_resolved_by_excludes(repo: Path) -> None:
    d = _build(
        Task(
            id="callers",
            description="x",
            owns_paths=["src/**"],
            excludes_paths=["src/auth/**"],
            depends_on=["contract"],
        ),
        Task(id="impl", description="y", owns_paths=["src/auth/**"], depends_on=["contract"]),
        contract_needed=True,
    )
    validate_decomposition(d, repo_path=repo)  # no raise


def test_validate_overlap_ok_when_dependent(repo: Path) -> None:
    """Two tasks that share files are fine if one depends on the other (sequential)."""
    d = _build(
        Task(id="impl", description="x", owns_paths=["src/auth/**"]),
        Task(
            id="cleanup",
            description="y",
            owns_paths=["src/auth/**"],
            depends_on=["impl"],
        ),
        kind=DecompositionKind.SEQUENTIAL,
        merge_order=["impl", "cleanup"],
    )
    validate_decomposition(d, repo_path=repo)  # no raise


def test_validate_skips_overlap_check_without_repo() -> None:
    """If caller doesn't pass repo_path, overlap check is silent."""
    d = _build(
        Task(id="a", description="x", owns_paths=["src/**"], depends_on=["contract"]),
        Task(id="b", description="y", owns_paths=["src/auth/**"], depends_on=["contract"]),
        contract_needed=True,
    )
    validate_decomposition(d)  # no raise


def test_validate_overlap_only_for_parallel(repo: Path) -> None:
    """Sequential decomposition: overlap is OK because tasks run in order."""
    d = _build(
        Task(id="a", description="x", owns_paths=["src/**"]),
        Task(id="b", description="y", owns_paths=["src/auth/**"], depends_on=["a"]),
        kind=DecompositionKind.SEQUENTIAL,
        merge_order=["a", "b"],
    )
    validate_decomposition(d, repo_path=repo)  # no raise


# ----- audit_ownership ------------------------------------------------------


import subprocess


@pytest.fixture
def worktree_repo(tmp_path: Path) -> Path:
    """Tiny git repo with one initial commit. Use as a 'worktree' for audit tests."""
    r = tmp_path / "wt"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "README.md").write_text("# r\n")
    subprocess.run(["git", "add", "README.md"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


def _commit_files(repo: Path, *files_with_content: tuple[str, str]) -> str:
    for path, content in files_with_content:
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
        subprocess.run(["git", "add", path], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feat"], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_audit_clean_when_in_lane(worktree_repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _commit_files(worktree_repo, ("src/auth/session.py", "x"), ("src/auth/__init__.py", ""))
    violations = audit_ownership(worktree_repo, base, ["src/auth/**"], [])
    assert violations == []


def test_audit_flags_out_of_lane(worktree_repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Specialist owns src/auth/** but also wrote a package.json at the root
    _commit_files(
        worktree_repo,
        ("src/auth/session.py", "x"),
        ("package.json", "{}"),
    )
    violations = audit_ownership(worktree_repo, base, ["src/auth/**"], [])
    assert violations == ["package.json"]


def test_audit_respects_excludes(worktree_repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _commit_files(
        worktree_repo,
        ("src/auth/session.py", "x"),       # in lane
        ("src/api/routes.py", "y"),         # in lane (src/**)
        ("src/auth/secret.py", "z"),        # excluded
    )
    violations = audit_ownership(
        worktree_repo, base,
        owns_paths=["src/**"], excludes_paths=["src/auth/secret.py"],
    )
    # secret.py is excluded → it's out-of-lane (specialist shouldn't have written it)
    assert violations == ["src/auth/secret.py"]


def test_audit_empty_owns_means_everything_is_out_of_lane(worktree_repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _commit_files(worktree_repo, ("a.txt", "x"), ("b.txt", "y"))
    violations = audit_ownership(worktree_repo, base, [], [])
    assert sorted(violations) == ["a.txt", "b.txt"]


def test_audit_no_changes_returns_empty(worktree_repo: Path) -> None:
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=worktree_repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # No new commits
    assert audit_ownership(worktree_repo, base, ["src/**"], []) == []
