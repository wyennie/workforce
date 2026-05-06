"""Project data model, on-disk store, and ID resolution.

A project is a registered git repo. Workforce identifies projects by a stable
12-hex ID derived from the repo's absolute path; if the user moves the repo,
we fall back to a `.workforce-project-id` marker file written at registration
time so memory survives the move.

Storage layout:

    <projects_root>/<project-id>/
        project.toml              # Project model
        memory/<specialist>.md    # per-specialist project memory
        missions/<mission-id>/    # mission artifacts (created at dispatch)
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from workforce import paths
from workforce.utils import _dump_toml

SCHEMA_VERSION = 1
ID_LENGTH = 12
ID_PATTERN = re.compile(rf"^[0-9a-f]{{{ID_LENGTH}}}$")
MARKER_FILENAME = ".workforce-project-id"

# Display name: 1-64 chars; letters, digits, spaces, _-., must start with
# alphanumeric so it doesn't look like a flag.
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\- ]{0,63}$")


class ProjectError(Exception):
    """Raised for project store errors that have a clear user-facing message.

    Always shown directly to the user via ``output.die``; do not include
    internal tracebacks or raw OS error text.
    """


# ----- ID resolution --------------------------------------------------------


def compute_project_id(repo_path: Path) -> str:
    """SHA-256 of the absolute repo path, first 12 hex chars."""
    abs_str = str(repo_path.resolve())
    return hashlib.sha256(abs_str.encode()).hexdigest()[:ID_LENGTH]


def marker_path(repo_path: Path) -> Path:
    """Return the path of the ``MARKER_FILENAME`` file inside ``repo_path``."""
    return repo_path / MARKER_FILENAME


def read_marker(repo_path: Path) -> str | None:
    """Read the project-id marker from inside the repo, if present.

    Returns:
        The 12-hex project id, or None if no marker file exists.

    Raises:
        ProjectError: If the marker file exists but contains an invalid id.
    """
    m = marker_path(repo_path)
    if not m.is_file():
        return None
    val = m.read_text().strip()
    if not ID_PATTERN.match(val):
        raise ProjectError(
            f"marker file at {m} contains invalid id: {val!r}"
        )
    return val


def write_marker(repo_path: Path, project_id: str) -> None:
    """Write a project-id marker file into the repo root.

    Raises:
        ValueError: If ``project_id`` doesn't match the expected hex format.
    """
    if not ID_PATTERN.match(project_id):
        raise ValueError(f"invalid project id: {project_id!r}")
    marker_path(repo_path).write_text(project_id + "\n")


def resolve_project_id(repo_path: Path) -> str:
    """Use the in-repo marker if present; else derive from the path."""
    existing = read_marker(repo_path)
    return existing or compute_project_id(repo_path)


def is_git_repo(repo_path: Path) -> bool:
    """True if `repo_path` is a git work tree (has `.git/` or a `.git` file)."""
    git = repo_path / ".git"
    return git.is_dir() or git.is_file()


# ----- Model ----------------------------------------------------------------


class Project(BaseModel):
    """A registered project with assigned specialists.

    Two kinds:
    - `repo` (default): a git work tree. Missions run in per-mission worktrees
      with commit-cadence rules and post-mission commit scanning.
    - `workspace`: a plain working directory. Missions run there directly with
      no worktree, no commit scanning, and no auto-merge — for recurring
      non-engineering tasks where outputs are files, not commits.

    Budget fields are optional. When `monthly_limit_usd` is set, dispatch will
    refuse new missions that would push the current calendar-month spend over the
    limit, and will warn when `alert_threshold_pct` percent of it has been used.
    `per_mission_limit_usd` caps the cost allowed for a single mission run
    (overrides the --max-cost CLI flag when lower).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    id: str
    name: str
    repo_path: str  # absolute path; for workspace kind, this is the working dir
    kind: Literal["repo", "workspace"] = "repo"
    assigned_specialists: list[str] = Field(default_factory=list)
    default_model: str | None = None
    # Budget controls (all optional; no enforcement when absent)
    monthly_limit_usd: float | None = None
    per_mission_limit_usd: float | None = None
    alert_threshold_pct: int = 80

    @field_validator("id")
    @classmethod
    def _valid_id(cls, v: str) -> str:
        if not ID_PATTERN.match(v):
            raise ValueError(f"invalid project id: {v!r}")
        return v

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not NAME_PATTERN.match(v):
            raise ValueError(
                "project name must be 1–64 chars, start with a letter or digit, "
                "and contain only letters, digits, spaces, '-', '_' or '.'"
            )
        return v


# ----- Per-project config (.workforce.toml) ---------------------------------


class ProjectConfig(BaseModel):
    """Per-project configuration loaded from ``.workforce.toml`` in the repo root.

    All fields are optional. Values present here serve as defaults for
    ``dispatch_command``, but explicit CLI flags always take precedence.
    """

    model_config = ConfigDict(extra="ignore")

    default_specialist: str | None = None
    review: bool | None = None
    auto_merge: bool | None = None
    max_turns: int | None = None
    max_cost: float | None = None


def load_project_config(repo_path: Path) -> "ProjectConfig":
    """Load ``.workforce.toml`` from *repo_path*, returning an empty config if absent.

    Args:
        repo_path: Root of the project directory (where ``.workforce.toml`` lives).

    Returns:
        A :class:`ProjectConfig` with values from the file, or a default-only
        instance if the file does not exist.
    """
    config_path = repo_path / ".workforce.toml"
    if not config_path.is_file():
        return ProjectConfig()
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    return ProjectConfig.model_validate(data)


# ----- Store ----------------------------------------------------------------


class ProjectStore:
    """File-backed CRUD for projects.

    Resolution: callers refer to projects by *display name* OR by full 12-hex
    ID via `resolve()`. Names must be unique across registered projects.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or paths.projects_dir()

    def _dir(self, project_id: str) -> Path:
        return self.root / project_id

    def _path(self, project_id: str) -> Path:
        return self._dir(project_id) / "project.toml"

    def memory_dir(self, project_id: str) -> Path:
        """Return the per-specialist memory directory for this project."""
        return self._dir(project_id) / "memory"

    def missions_dir(self, project_id: str) -> Path:
        """Return the missions artifact directory for this project."""
        return self._dir(project_id) / "missions"

    def exists(self, project_id: str) -> bool:
        """Return True if a ``project.toml`` file exists for ``project_id``."""
        return self._path(project_id).is_file()

    def ids(self) -> list[str]:
        """Return sorted list of all registered project ids."""
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and (p / "project.toml").is_file()
        )

    def list(self) -> list[Project]:
        """Return all registered projects, sorted by id."""
        return [self.load_by_id(i) for i in self.ids()]

    def load_by_id(self, project_id: str) -> Project:
        """Load a Project by its 12-hex id.

        Raises:
            ProjectError: If no project with that id is registered.
        """
        path = self._path(project_id)
        if not path.is_file():
            raise ProjectError(f"no such project id: {project_id!r}")
        with path.open("rb") as f:
            data = tomllib.load(f)
        return Project.model_validate(data)

    def find_by_name(self, name: str) -> Project:
        """Find a project by display name (case-insensitive).

        Raises:
            ProjectError: If no project has that name, or if multiple projects
                share the same name (name uniqueness is enforced at save time
                but can drift if the store is edited manually).
        """
        matches = [p for p in self.list() if p.name.lower() == name.lower()]
        if not matches:
            raise ProjectError(f"no project named {name!r}")
        if len(matches) > 1:
            ids = ", ".join(p.id for p in matches)
            raise ProjectError(
                f"name {name!r} matches multiple projects ({ids}); refer by id"
            )
        return matches[0]

    def find_by_cwd(self, start_path: Path | None = None) -> "Project | None":
        """Walk up from *start_path* (default: cwd) to find a ``.workforce-project-id`` marker.

        Returns the registered Project whose id matches the first marker found
        while ascending the directory tree, or ``None`` if no match is found.

        Args:
            start_path: Directory to start from.  Defaults to ``Path.cwd()``.
        """
        current = (start_path or Path.cwd()).resolve()
        while True:
            try:
                project_id = read_marker(current)
            except ProjectError:
                project_id = None
            if project_id is not None and self.exists(project_id):
                return self.load_by_id(project_id)
            parent = current.parent
            if parent == current:
                break
            current = parent
        return None

    def resolve(self, ref: str) -> Project:
        """Resolve a project by full ID (12 hex), display name, or ``'.'``/``''`` for cwd auto-detect.

        When *ref* is ``'.'`` or the empty string, :meth:`find_by_cwd` is called.
        A :class:`ProjectError` is raised if no project can be detected from the
        current working directory.
        """
        if ref in (".", ""):
            proj = self.find_by_cwd()
            if proj is None:
                raise ProjectError(
                    f"could not auto-detect project from current directory; "
                    f"no {MARKER_FILENAME!r} file found in this directory or any parent. "
                    "Register this directory with `workforce project add .` first."
                )
            return proj
        if ID_PATTERN.match(ref):
            return self.load_by_id(ref)
        return self.find_by_name(ref)

    def save(self, project: Project, *, overwrite: bool = False) -> None:
        """Persist a Project to ``project.toml``.

        Creates the project directory, memory dir, and missions dir if absent.
        When ``overwrite=False`` (default), rejects duplicate ids and duplicate
        display names.

        Args:
            project: The Project to save.
            overwrite: If True, update an existing project in place (skips
                name-uniqueness check).

        Raises:
            ProjectError: If the id or display name already exists and
                ``overwrite`` is False.
        """
        if self.exists(project.id) and not overwrite:
            raise ProjectError(f"project {project.id!r} is already registered")
        if not overwrite:
            # Reject duplicate display names at registration time.
            for existing in self.list():
                if existing.name.lower() == project.name.lower():
                    raise ProjectError(
                        f"project name {project.name!r} is already used by id "
                        f"{existing.id!r} ({existing.repo_path})"
                    )
        d = self._dir(project.id)
        d.mkdir(parents=True, exist_ok=True)
        self.memory_dir(project.id).mkdir(exist_ok=True)
        self.missions_dir(project.id).mkdir(exist_ok=True)
        self._path(project.id).write_text(
            _dump_toml(project.model_dump(exclude_none=True))
        )

    def delete(self, project_id: str) -> None:
        """Remove a project's entire data directory (missions, memory, TOML).

        Does not touch the source repo or its marker file.

        Raises:
            ProjectError: If the project id isn't registered.
        """
        if not self.exists(project_id):
            raise ProjectError(f"no such project id: {project_id!r}")
        shutil.rmtree(self._dir(project_id))


