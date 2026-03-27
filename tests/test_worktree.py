from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from workforce.worktree import (
    BRANCH_PREFIX,
    BranchExistsError,
    RepoNotCleanError,
    UnbornRepoError,
    WorktreeError,
    WorktreeManager,
    find_workforce_branches,
    has_commits,
)


PROJECT_ID = "abc123def456"


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with one commit, isolated from the user's git config."""
    r = tmp_path / "repo"
    r.mkdir()
    _run(["git", "init", "-q", "-b", "main"], r)
    _run(["git", "config", "user.email", "test@example.com"], r)
    _run(["git", "config", "user.name", "Test"], r)
    (r / "README.md").write_text("# test\n")
    _run(["git", "add", "README.md"], r)
    _run(["git", "commit", "-q", "-m", "initial"], r)
    return r


@pytest.fixture
def manager(tmp_path: Path) -> WorktreeManager:
    return WorktreeManager(projects_root=tmp_path / "wf-projects")


# ----- create ----------------------------------------------------------------


def test_create_succeeds_on_clean_repo(repo: Path, manager: WorktreeManager) -> None:
    ref = manager.create(repo, PROJECT_ID, "m001")
    assert ref.worktree_path.is_dir()
    assert (ref.worktree_path / "README.md").is_file()
    assert ref.branch == BRANCH_PREFIX + "m001"

    # Verify the worktree is on the new branch
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=ref.worktree_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == ref.branch


def test_create_path_layout(repo: Path, manager: WorktreeManager) -> None:
    ref = manager.create(repo, PROJECT_ID, "m001")
    expected = manager.projects_root / PROJECT_ID / "worktrees" / "m001"
    assert ref.worktree_path == expected


def test_create_refuses_dirty_repo(repo: Path, manager: WorktreeManager) -> None:
    (repo / "README.md").write_text("# changed\n")
    with pytest.raises(RepoNotCleanError, match="uncommitted changes"):
        manager.create(repo, PROJECT_ID, "m001")


def test_create_refuses_staged_repo(repo: Path, manager: WorktreeManager) -> None:
    (repo / "new.txt").write_text("x")
    _run(["git", "add", "new.txt"], repo)
    with pytest.raises(RepoNotCleanError):
        manager.create(repo, PROJECT_ID, "m001")


def test_create_tolerates_untracked_files(repo: Path, manager: WorktreeManager) -> None:
    (repo / "scratch.txt").write_text("local-only")
    ref = manager.create(repo, PROJECT_ID, "m001")
    assert ref.worktree_path.is_dir()
    # Untracked file in source should NOT appear in the worktree (it wasn't committed).
    assert not (ref.worktree_path / "scratch.txt").exists()


def test_create_refuses_non_git_path(tmp_path: Path, manager: WorktreeManager) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError, match="not a git repository"):
        manager.create(plain, PROJECT_ID, "m001")


def test_create_refuses_unborn_repo(tmp_path: Path, manager: WorktreeManager) -> None:
    """A repo with `git init` but no commits can't host worktrees."""
    r = tmp_path / "unborn"
    r.mkdir()
    _run(["git", "init", "-q", "-b", "main"], r)
    with pytest.raises(UnbornRepoError, match="no commits yet"):
        manager.create(r, PROJECT_ID, "m001")


def test_has_commits_true_after_commit(repo: Path) -> None:
    assert has_commits(repo) is True


def test_has_commits_false_when_unborn(tmp_path: Path) -> None:
    r = tmp_path / "unborn"
    r.mkdir()
    _run(["git", "init", "-q", "-b", "main"], r)
    assert has_commits(r) is False


# ----- find_workforce_branches ---------------------------------------------


def test_find_workforce_branches_lists_all(repo: Path, manager: WorktreeManager) -> None:
    manager.create(repo, PROJECT_ID, "m001")
    manager.create(repo, PROJECT_ID, "m002")
    # Create a non-workforce branch too — should be ignored
    _run(["git", "branch", "feature-x"], repo)
    branches = find_workforce_branches(repo)
    assert branches == [
        f"{BRANCH_PREFIX}m001",
        f"{BRANCH_PREFIX}m002",
    ]


def test_find_workforce_branches_filters_to_merged(repo: Path, manager: WorktreeManager) -> None:
    """Only branches reachable from the target are returned."""
    # Create two workforce branches with a commit each
    ref_a = manager.create(repo, PROJECT_ID, "m001")
    (ref_a.worktree_path / "a.txt").write_text("a\n")
    _run(["git", "add", "a.txt"], ref_a.worktree_path)
    _run(["git", "commit", "-q", "-m", "feat: a"], ref_a.worktree_path)

    ref_b = manager.create(repo, PROJECT_ID, "m002")
    (ref_b.worktree_path / "b.txt").write_text("b\n")
    _run(["git", "add", "b.txt"], ref_b.worktree_path)
    _run(["git", "commit", "-q", "-m", "feat: b"], ref_b.worktree_path)

    # Merge only m001 into main
    _run(["git", "merge", "--no-ff", ref_a.branch], repo)

    merged = find_workforce_branches(repo, merged_into="main")
    assert ref_a.branch in merged
    assert ref_b.branch not in merged


def test_find_workforce_branches_empty_when_none_exist(repo: Path) -> None:
    assert find_workforce_branches(repo) == []


def test_create_refuses_existing_path(repo: Path, manager: WorktreeManager) -> None:
    manager.create(repo, PROJECT_ID, "m001")
    with pytest.raises(WorktreeError, match="already exists"):
        manager.create(repo, PROJECT_ID, "m001")


def test_create_refuses_existing_branch(repo: Path, manager: WorktreeManager) -> None:
    _run(["git", "branch", BRANCH_PREFIX + "m001"], repo)
    with pytest.raises(BranchExistsError, match="already exists"):
        manager.create(repo, PROJECT_ID, "m001")


@pytest.mark.parametrize(
    "bad",
    ["", "-leading", ".dot", "has space", "a/b", "x" * 65],
)
def test_create_rejects_bad_mission_id(repo: Path, manager: WorktreeManager, bad: str) -> None:
    with pytest.raises(WorktreeError, match="invalid mission id"):
        manager.create(repo, PROJECT_ID, bad)


# ----- remove / prune --------------------------------------------------------


def test_remove_clean_worktree(repo: Path, manager: WorktreeManager) -> None:
    ref = manager.create(repo, PROJECT_ID, "m001")
    manager.remove(repo, ref.worktree_path)
    assert not ref.worktree_path.exists()
    entries = manager.list_git_worktrees(repo)
    paths = [e.path for e in entries]
    assert ref.worktree_path not in paths


def test_remove_dirty_worktree_requires_force(repo: Path, manager: WorktreeManager) -> None:
    ref = manager.create(repo, PROJECT_ID, "m001")
    (ref.worktree_path / "dirty.txt").write_text("x")
    _run(["git", "add", "dirty.txt"], ref.worktree_path)
    with pytest.raises(WorktreeError):
        manager.remove(repo, ref.worktree_path)
    # Force succeeds.
    manager.remove(repo, ref.worktree_path, force=True)
    assert not ref.worktree_path.exists()


def test_prune_succeeds_on_clean_state(repo: Path, manager: WorktreeManager) -> None:
    manager.prune(repo)  # no-op, should not raise


def test_prune_after_manual_dir_removal(repo: Path, manager: WorktreeManager) -> None:
    """If a worktree dir is rm'd out from under git, prune should clean the registry."""
    import shutil
    ref = manager.create(repo, PROJECT_ID, "m001")
    shutil.rmtree(ref.worktree_path)
    manager.prune(repo)
    paths = [e.path for e in manager.list_git_worktrees(repo)]
    assert ref.worktree_path not in paths


# ----- listing ---------------------------------------------------------------


def test_list_for_project_empty(manager: WorktreeManager) -> None:
    assert manager.list_for_project(PROJECT_ID) == []


def test_list_for_project_returns_dirs(repo: Path, manager: WorktreeManager) -> None:
    a = manager.create(repo, PROJECT_ID, "m001")
    b = manager.create(repo, PROJECT_ID, "m002")
    paths = manager.list_for_project(PROJECT_ID)
    assert a.worktree_path in paths
    assert b.worktree_path in paths
    assert paths == sorted(paths)


def test_list_git_worktrees_includes_source(repo: Path, manager: WorktreeManager) -> None:
    entries = manager.list_git_worktrees(repo)
    assert any(e.path == repo for e in entries)


def test_list_git_worktrees_after_create(repo: Path, manager: WorktreeManager) -> None:
    ref = manager.create(repo, PROJECT_ID, "m001")
    entries = manager.list_git_worktrees(repo)
    paths = [e.path for e in entries]
    assert ref.worktree_path in paths
    branches = [e.branch for e in entries if e.path == ref.worktree_path]
    assert branches == [f"refs/heads/{ref.branch}"]
