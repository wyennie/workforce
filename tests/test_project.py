from __future__ import annotations

from pathlib import Path

import pytest

from workforce.project import (
    ID_LENGTH,
    MARKER_FILENAME,
    Project,
    ProjectConfig,
    ProjectError,
    ProjectStore,
    compute_project_id,
    is_git_repo,
    load_project_config,
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


def test_project_kind_defaults_to_repo() -> None:
    p = Project(id="abc123def456", name="x", repo_path="/tmp/x")
    assert p.kind == "repo"


def test_project_kind_workspace_explicit() -> None:
    p = Project(id="abc123def456", name="x", repo_path="/tmp/x", kind="workspace")
    assert p.kind == "workspace"


def test_project_kind_rejects_unknown_value() -> None:
    with pytest.raises(ValueError):
        Project(id="abc123def456", name="x", repo_path="/tmp/x", kind="bogus")  # type: ignore[arg-type]


def test_workspace_kind_roundtrip(store: ProjectStore) -> None:
    p = Project(id="abc123def456", name="myws", repo_path="/tmp/x", kind="workspace")
    store.save(p)
    loaded = store.load_by_id(p.id)
    assert loaded.kind == "workspace"
    assert loaded == p


def test_kind_defaults_to_repo_for_legacy_toml(store: ProjectStore) -> None:
    """Existing project.toml files written before `kind` existed must still load."""
    pid = "abc123def456"
    project_dir = store._dir(pid)
    project_dir.mkdir(parents=True)
    (project_dir / "project.toml").write_text(
        f'schema_version = 1\nid = "{pid}"\nname = "legacy"\nrepo_path = "/tmp/legacy"\n'
        'assigned_specialists = []\n'
    )
    loaded = store.load_by_id(pid)
    assert loaded.kind == "repo"


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


# ----- find_by_cwd ----------------------------------------------------------


def test_find_by_cwd_returns_project(tmp_path: Path) -> None:
    """find_by_cwd() from a deep subdir walks up to find the marker file."""
    # Set up a store and register a project
    store = ProjectStore(root=tmp_path / "projects")
    repo = tmp_path / "repo"
    repo.mkdir()

    # Give the repo a deterministic id via a marker file
    project_id = compute_project_id(repo)
    write_marker(repo, project_id)

    proj = Project(id=project_id, name="autoproj", repo_path=str(repo))
    store.save(proj)

    # Create a deep subdirectory and call find_by_cwd from there
    deep = repo / "src" / "pkg" / "sub"
    deep.mkdir(parents=True)

    found = store.find_by_cwd(start_path=deep)
    assert found is not None
    assert found.id == project_id
    assert found.name == "autoproj"


def test_find_by_cwd_returns_none_when_no_marker(tmp_path: Path) -> None:
    """find_by_cwd() returns None when no marker file exists anywhere."""
    store = ProjectStore(root=tmp_path / "projects")
    result = store.find_by_cwd(start_path=tmp_path / "unrelated")
    assert result is None


def test_find_by_cwd_ignores_unregistered_marker(tmp_path: Path) -> None:
    """find_by_cwd() returns None when marker exists but id is not in the store."""
    store = ProjectStore(root=tmp_path / "projects")
    repo = tmp_path / "repo"
    repo.mkdir()
    write_marker(repo, compute_project_id(repo))
    # Don't save any project to the store
    result = store.find_by_cwd(start_path=repo)
    assert result is None


def test_resolve_dot_finds_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve('.') auto-detects the project from the cwd marker."""
    store = ProjectStore(root=tmp_path / "projects")
    repo = tmp_path / "repo"
    repo.mkdir()
    project_id = compute_project_id(repo)
    write_marker(repo, project_id)
    proj = Project(id=project_id, name="dotproj", repo_path=str(repo))
    store.save(proj)

    monkeypatch.chdir(repo)
    found = store.resolve(".")
    assert found.id == project_id


def test_resolve_empty_string_finds_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve('') also triggers cwd auto-detection."""
    store = ProjectStore(root=tmp_path / "projects")
    repo = tmp_path / "repo"
    repo.mkdir()
    project_id = compute_project_id(repo)
    write_marker(repo, project_id)
    proj = Project(id=project_id, name="emptyproj", repo_path=str(repo))
    store.save(proj)

    monkeypatch.chdir(repo)
    found = store.resolve("")
    assert found.id == project_id


def test_resolve_dot_raises_when_no_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """resolve('.') raises ProjectError when no marker is found."""
    store = ProjectStore(root=tmp_path / "projects")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ProjectError, match="auto-detect"):
        store.resolve(".")


# ----- ProjectConfig and load_project_config --------------------------------


def test_project_config_defaults() -> None:
    """Empty ProjectConfig has all None fields."""
    cfg = ProjectConfig()
    assert cfg.default_specialist is None
    assert cfg.review is None
    assert cfg.auto_merge is None
    assert cfg.max_turns is None
    assert cfg.max_cost is None


def test_load_project_config_missing_file(tmp_path: Path) -> None:
    """Returns default ProjectConfig when .workforce.toml is absent."""
    cfg = load_project_config(tmp_path)
    assert cfg == ProjectConfig()


def test_load_project_config_reads_values(tmp_path: Path) -> None:
    """Values in .workforce.toml are loaded into the ProjectConfig."""
    (tmp_path / ".workforce.toml").write_text(
        "default_specialist = 'builder'\n"
        "review = true\n"
        "auto_merge = false\n"
        "max_turns = 30\n"
        "max_cost = 2.5\n"
    )
    cfg = load_project_config(tmp_path)
    assert cfg.default_specialist == "builder"
    assert cfg.review is True
    assert cfg.auto_merge is False
    assert cfg.max_turns == 30
    assert cfg.max_cost == 2.5


def test_load_project_config_ignores_unknown_keys(tmp_path: Path) -> None:
    """Extra keys in .workforce.toml are silently ignored (extra='ignore')."""
    (tmp_path / ".workforce.toml").write_text("unknown_key = 'ignored'\nmax_turns = 10\n")
    cfg = load_project_config(tmp_path)
    assert cfg.max_turns == 10


def test_load_project_config_partial(tmp_path: Path) -> None:
    """Partial .workforce.toml leaves unset fields as None."""
    (tmp_path / ".workforce.toml").write_text("review = true\n")
    cfg = load_project_config(tmp_path)
    assert cfg.review is True
    assert cfg.max_turns is None
