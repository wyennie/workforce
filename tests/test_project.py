from __future__ import annotations

from pathlib import Path

import pytest

from workforce.project import (
    ID_LENGTH,
    MARKER_FILENAME,
    Project,
    ProjectError,
    ProjectStore,
    compute_project_id,
    is_git_repo,
    read_marker,
    resolve_project_id,
    write_marker,
)


@pytest.fixture
def store(tmp_path: Path) -> ProjectStore:
    return ProjectStore(root=tmp_path / "projects")


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """A directory that looks enough like a git repo to satisfy is_git_repo."""
    repo = tmp_path / "myapp"
    repo.mkdir()
    (repo / ".git").mkdir()
    return repo


# ----- ID computation -------------------------------------------------------


def test_compute_id_is_deterministic(tmp_path: Path) -> None:
    p = tmp_path / "x"
    p.mkdir()
    assert compute_project_id(p) == compute_project_id(p)


def test_compute_id_length_and_charset(tmp_path: Path) -> None:
    p = tmp_path / "x"
    p.mkdir()
    pid = compute_project_id(p)
    assert len(pid) == ID_LENGTH
    assert all(c in "0123456789abcdef" for c in pid)


def test_compute_id_differs_by_path(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert compute_project_id(a) != compute_project_id(b)


# ----- Marker file ----------------------------------------------------------


def test_marker_roundtrip(tmp_path: Path) -> None:
    write_marker(tmp_path, "abc123def456")
    assert read_marker(tmp_path) == "abc123def456"


def test_marker_absent_returns_none(tmp_path: Path) -> None:
    assert read_marker(tmp_path) is None


def test_marker_invalid_raises(tmp_path: Path) -> None:
    (tmp_path / MARKER_FILENAME).write_text("not-a-real-id\n")
    with pytest.raises(ProjectError, match="invalid id"):
        read_marker(tmp_path)


def test_write_marker_rejects_bad_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_marker(tmp_path, "TOO-SHORT")


def test_resolve_uses_marker_when_present(tmp_path: Path) -> None:
    write_marker(tmp_path, "abc123def456")
    assert resolve_project_id(tmp_path) == "abc123def456"


def test_resolve_falls_back_to_path_hash(tmp_path: Path) -> None:
    assert resolve_project_id(tmp_path) == compute_project_id(tmp_path)


def test_resolve_survives_repo_move(tmp_path: Path) -> None:
    """If the repo moves but the marker travels with it, the id is stable."""
    a = tmp_path / "first"
    b = tmp_path / "second"
    a.mkdir()
    write_marker(a, "abc123def456")
    a.rename(b)
    assert resolve_project_id(b) == "abc123def456"


# ----- is_git_repo ----------------------------------------------------------


def test_is_git_repo_true_for_git_dir(fake_repo: Path) -> None:
    assert is_git_repo(fake_repo)


def test_is_git_repo_true_for_git_file(tmp_path: Path) -> None:
    """Submodules and worktrees use a `.git` *file*, not a directory."""
    (tmp_path / ".git").write_text("gitdir: ../mainrepo/.git/worktrees/x\n")
    assert is_git_repo(tmp_path)


def test_is_git_repo_false_for_plain_dir(tmp_path: Path) -> None:
    assert not is_git_repo(tmp_path)


# ----- Model validation -----------------------------------------------------


def test_project_requires_valid_id() -> None:
    with pytest.raises(ValueError, match="invalid project id"):
        Project(id="bad", name="x", repo_path="/tmp/x")


def test_project_requires_valid_name() -> None:
    with pytest.raises(ValueError, match="project name"):
        Project(id="abc123def456", name=" leading-space", repo_path="/tmp/x")


def test_project_extra_fields_forbidden() -> None:
    with pytest.raises(ValueError):
        Project.model_validate(
            {
                "id": "abc123def456",
                "name": "x",
                "repo_path": "/tmp/x",
                "wat": True,
            }
        )


# ----- Store ----------------------------------------------------------------


def _make(name: str = "myapp", id: str = "abc123def456", path: str = "/tmp/x") -> Project:
    return Project(id=id, name=name, repo_path=path)


def test_save_and_load_roundtrip(store: ProjectStore) -> None:
    p = _make()
    store.save(p)
    loaded = store.load_by_id(p.id)
    assert loaded == p


def test_save_creates_subdirs(store: ProjectStore) -> None:
    p = _make()
    store.save(p)
    assert store.memory_dir(p.id).is_dir()
    assert store.missions_dir(p.id).is_dir()


def test_save_refuses_duplicate_id(store: ProjectStore) -> None:
    store.save(_make())
    with pytest.raises(ProjectError, match="already registered"):
        store.save(_make())


def test_save_refuses_duplicate_name(store: ProjectStore) -> None:
    store.save(_make(name="myapp", id="abc123def456"))
    with pytest.raises(ProjectError, match="already used"):
        store.save(_make(name="myapp", id="111111111111"))


def test_save_overwrite_allowed(store: ProjectStore) -> None:
    p = _make()
    store.save(p)
    p.assigned_specialists.append("aria")
    store.save(p, overwrite=True)
    assert store.load_by_id(p.id).assigned_specialists == ["aria"]


def test_resolve_by_name(store: ProjectStore) -> None:
    p = _make()
    store.save(p)
    assert store.resolve("myapp").id == p.id


def test_resolve_by_name_case_insensitive(store: ProjectStore) -> None:
    p = _make(name="MyApp")
    store.save(p)
    assert store.resolve("myapp").id == p.id


def test_resolve_by_id(store: ProjectStore) -> None:
    p = _make()
    store.save(p)
    assert store.resolve("abc123def456").name == "myapp"


def test_resolve_unknown_name(store: ProjectStore) -> None:
    with pytest.raises(ProjectError, match="no project named"):
        store.resolve("ghost")


def test_resolve_unknown_id(store: ProjectStore) -> None:
    with pytest.raises(ProjectError, match="no such project id"):
        store.resolve("000000000000")


def test_ids_sorted(store: ProjectStore) -> None:
    store.save(_make(id="ccc111111111", name="c"))
    store.save(_make(id="aaa111111111", name="a"))
    store.save(_make(id="bbb111111111", name="b"))
    assert store.ids() == ["aaa111111111", "bbb111111111", "ccc111111111"]


def test_delete_removes_dir(store: ProjectStore) -> None:
    p = _make()
    store.save(p)
    store.delete(p.id)
    assert not store.exists(p.id)


def test_delete_unknown_raises(store: ProjectStore) -> None:
    with pytest.raises(ProjectError):
        store.delete("000000000000")
