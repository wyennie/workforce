"""Git worktree manager.

Each mission runs in its own worktree on a fresh branch named
`workforce/<mission-id>`. Worktrees live under
`<projects_dir>/<project-id>/worktrees/<mission-id>` — far from the user's
source tree so editor file watchers and `find`/`grep` aren't polluted.

We shell out to `git` directly. GitPython is overkill for this surface and
adds a runtime dep with little upside.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from workforce import paths
from workforce.project import is_git_repo

BRANCH_PREFIX = "workforce/"
# mission_id is used in a branch name and a directory name. Keep it strict.
MISSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


class WorktreeError(Exception):
    """Worktree operation failed; message is user-facing.

    Raised by :class:`WorktreeManager` and the standalone git helpers.
    Always displayed directly to the user via ``output.die``; do not embed
    technical details that aren't meaningful without code context.
    """


class RepoNotCleanError(WorktreeError):
    """Source repo has staged/modified changes (untracked files are tolerated).

    Raised during worktree creation to prevent forking from a dirty tree,
    which would leave the specialist with an ambiguous starting state.
    """


class BranchExistsError(WorktreeError):
    """A branch with the target name already exists in the source repo.

    Each mission must use a unique branch name; this error signals an id
    collision (extremely unlikely but possible if the clock has low resolution
    or a mission is retried with the same id).
    """


class UnbornRepoError(WorktreeError):
    """Source repo has no commits yet — can't fork a worktree from nothing."""


def has_commits(repo_path: Path) -> bool:
    """True if the repo has at least one commit reachable from HEAD."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def is_repo_clean(repo_path: Path) -> tuple[bool, list[str]]:
    """True iff the repo has no staged/modified files (untracked OK).

    Returns (clean?, list of dirty paths). The dirty list is for the caller
    to surface in error messages — same policy as worktree creation.
    """
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    ).stdout
    dirty: list[str] = []
    for line in out.splitlines():
        if not line or line.startswith("??"):
            continue
        dirty.append(line[3:])
    return (not dirty, dirty)


def ensure_branch(repo_path: Path, name: str) -> None:
    """Make sure `name` exists as a local branch, creating it from current HEAD if not.

    No-op if it already exists. Used by `--branch` so the user doesn't have to
    pre-create the staging branch. The branch is created at the repo's current
    HEAD; we do not sync from main or any other ref.

    Raises `WorktreeError` on failure (e.g. the repo has no commits, or git
    refuses the name).
    """
    if not has_commits(repo_path):
        raise UnbornRepoError(
            f"{repo_path} has no commits yet — can't create a branch from nothing. "
            "Make at least one commit first."
        )
    exists = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
        cwd=repo_path, capture_output=True, text=True,
    ).returncode == 0
    if exists:
        return
    r = subprocess.run(
        ["git", "branch", name],
        cwd=repo_path, capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise WorktreeError(
            f"could not create branch {name!r} in {repo_path}: "
            + (r.stderr.strip() or r.stdout.strip())
        )


def current_branch(repo_path: Path) -> str | None:
    """Return the short name of the currently checked-out branch, or None if detached."""
    r = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=repo_path, capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def is_clean(repo_path: Path) -> bool:
    """True iff `git status --porcelain` has no staged/modified entries.

    Untracked files (lines starting with '??') are tolerated, matching the
    worktree manager creation policy.
    """
    out = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True, check=True,
    ).stdout
    return all(line.startswith("??") for line in out.splitlines() if line)


def find_workforce_branches(repo_path: Path, *, merged_into: str | None = None) -> list[str]:
    """List `workforce/*` branches in `repo_path`.

    If `merged_into` is given, return only those whose tip is reachable from
    that branch (i.e. fully merged). If None, return all `workforce/*` branches.
    """
    args = ["branch", "--list", f"{BRANCH_PREFIX}*"]
    if merged_into is not None:
        args.insert(1, "--merged")
        args.insert(2, merged_into)
    out = subprocess.run(
        ["git", *args],
        cwd=repo_path, capture_output=True, text=True, check=True,
    ).stdout
    branches: list[str] = []
    for line in out.splitlines():
        # `git branch` prefixes with "* " (current), "+ " (worktree), or "  "
        name = line.lstrip("*+ ").strip()
        if name.startswith(BRANCH_PREFIX):
            branches.append(name)
    return sorted(branches)


@dataclass(frozen=True)
class WorktreeRef:
    """Result of a successful :meth:`WorktreeManager.create` call.

    Attributes:
        repo_path: Path to the source repository.
        worktree_path: Path to the newly created worktree directory.
        branch: Full branch name (``workforce/<mission-id>``).
        mission_id: The mission id used to name the branch and directory.
        base_sha: The commit SHA the worktree was forked from; used by
            :func:`scan_commits` to diff which commits the specialist added.
    """

    repo_path: Path
    worktree_path: Path
    branch: str
    mission_id: str
    base_sha: str


@dataclass(frozen=True)
class WorktreeListEntry:
    """One row from `git worktree list --porcelain`."""
    path: Path
    head: str | None       # commit SHA, or None if bare/unborn
    branch: str | None     # full ref name (refs/heads/...) or None if detached
    is_locked: bool


class WorktreeManager:
    """Per-project worktree CRUD.

    Stateless — operates on disk directly. Callers pass repo_path + project_id
    + mission_id; the manager works out the rest.
    """

    def __init__(self, projects_root: Path | None = None) -> None:
        self.projects_root = projects_root or paths.projects_dir()

    # ----- path helpers -----------------------------------------------------

    def project_worktrees_dir(self, project_id: str) -> Path:
        return self.projects_root / project_id / "worktrees"

    def worktree_path(self, project_id: str, mission_id: str) -> Path:
        return self.project_worktrees_dir(project_id) / mission_id

    def branch_name(self, mission_id: str) -> str:
        return BRANCH_PREFIX + mission_id

    # ----- create ------------------------------------------------------------

    def create(
        self,
        repo_path: Path,
        project_id: str,
        mission_id: str,
        *,
        start_point: str | None = None,
    ) -> WorktreeRef:
        """Create a fresh worktree on a new `workforce/<mission-id>` branch.

        With `start_point=None` (default), the new branch forks from the source
        repo's current HEAD. With a ref name (a branch, sha, or tag), it forks
        from there instead — used for sequential execution where later tasks
        fork from earlier tasks' branch tips.
        """
        if not MISSION_ID_PATTERN.match(mission_id):
            raise WorktreeError(
                f"invalid mission id {mission_id!r}: must start with alphanumeric "
                "and contain only [A-Za-z0-9._-] (max 64 chars)"
            )
        if not is_git_repo(repo_path):
            raise WorktreeError(f"{repo_path} is not a git repository")

        if not has_commits(repo_path):
            raise UnbornRepoError(
                f"{repo_path} has no commits yet — workforce can't fork a "
                "worktree from an empty repo. Make at least one commit "
                "(e.g. `git commit --allow-empty -m initial`) and try again."
            )

        dirty = self._dirty_paths(repo_path)
        if dirty:
            preview = ", ".join(dirty[:3]) + ("..." if len(dirty) > 3 else "")
            raise RepoNotCleanError(
                f"{repo_path} has uncommitted changes ({preview}). "
                "Commit or stash before dispatching. (Untracked files are OK.)"
            )

        wt_path = self.worktree_path(project_id, mission_id)
        if wt_path.exists():
            raise WorktreeError(f"worktree path already exists: {wt_path}")

        branch = self.branch_name(mission_id)
        if self._branch_exists(repo_path, branch):
            raise BranchExistsError(
                f"branch {branch!r} already exists in {repo_path}. "
                "Pick a different mission id or delete the branch."
            )

        wt_path.parent.mkdir(parents=True, exist_ok=True)
        if start_point is not None:
            base_ref = start_point
        else:
            base_ref = "HEAD"
        base_sha = self._git(repo_path, ["rev-parse", base_ref], capture=True).strip()
        # `git worktree add -b NEW PATH START_POINT` forks NEW from START_POINT.
        # Without the start_point argument it forks from current HEAD.
        args = ["worktree", "add", "-b", branch, str(wt_path)]
        if start_point is not None:
            args.append(start_point)
        self._git(repo_path, args)
        return WorktreeRef(
            repo_path=repo_path,
            worktree_path=wt_path,
            branch=branch,
            mission_id=mission_id,
            base_sha=base_sha,
        )

    # ----- remove / prune ----------------------------------------------------

    def remove(
        self,
        repo_path: Path,
        worktree_path: Path,
        *,
        force: bool = False,
    ) -> None:
        """Remove a worktree via `git worktree remove`.

        With force=True, also removes uncommitted changes inside it.
        Always best-effort cleans the directory afterwards in case git left it.
        """
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree_path))
        try:
            self._git(repo_path, args)
        except WorktreeError:
            # Fall through to filesystem cleanup. We still raise if the dir
            # remains.
            pass

        if worktree_path.exists():
            if force:
                shutil.rmtree(worktree_path, ignore_errors=True)
            else:
                raise WorktreeError(
                    f"worktree at {worktree_path} has uncommitted changes or "
                    "untracked files; pass force=True (CLI: --force) to remove anyway."
                )

    def prune(self, repo_path: Path) -> None:
        """`git worktree prune` — drops registry entries whose dirs are gone."""
        self._git(repo_path, ["worktree", "prune"])

    # ----- listing -----------------------------------------------------------

    def list_for_project(self, project_id: str) -> list[Path]:
        """Worktree directories that exist on disk for this project."""
        d = self.project_worktrees_dir(project_id)
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir() if p.is_dir())

    def list_git_worktrees(self, repo_path: Path) -> list[WorktreeListEntry]:
        """Parse `git worktree list --porcelain`. Includes the source itself."""
        out = self._git(repo_path, ["worktree", "list", "--porcelain"], capture=True)
        return _parse_porcelain(out)

    # ----- internals ---------------------------------------------------------

    def _dirty_paths(self, repo_path: Path) -> list[str]:
        """Return modified/staged file paths (untracked files filtered out)."""
        out = self._git(repo_path, ["status", "--porcelain"], capture=True)
        dirty: list[str] = []
        for line in out.splitlines():
            if not line:
                continue
            # Porcelain format: "XY filename". '??' = untracked, which we tolerate.
            if line.startswith("??"):
                continue
            # Stripped to just the filename for the error preview.
            dirty.append(line[3:])
        return dirty

    def _branch_exists(self, repo_path: Path, branch: str) -> bool:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _git(
        self,
        repo_path: Path,
        args: list[str],
        *,
        capture: bool = False,
    ) -> str:
        """Run a git subcommand in `repo_path`. Raise WorktreeError on failure."""
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as e:
            raise WorktreeError(f"git invocation failed: {e}") from e
        if result.returncode != 0:
            raise WorktreeError(
                f"git {' '.join(args)} failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result.stdout if capture else ""


def _parse_porcelain(out: str) -> list[WorktreeListEntry]:
    """Parse `git worktree list --porcelain` output into entries.

    Format is records separated by blank lines; each record contains lines
    like `worktree <path>`, `HEAD <sha>`, `branch <ref>`, `bare`, `detached`,
    `locked [<reason>]`.
    """
    entries: list[WorktreeListEntry] = []
    current: dict[str, object] = {}

    def flush() -> None:
        if "path" in current:
            entries.append(
                WorktreeListEntry(
                    path=Path(str(current["path"])),
                    head=str(current["head"]) if "head" in current else None,
                    branch=str(current["branch"]) if "branch" in current else None,
                    is_locked=bool(current.get("locked", False)),
                )
            )
        current.clear()

    for line in out.splitlines():
        if not line.strip():
            flush()
            continue
        parts = line.split(" ", 1)
        key = parts[0]
        val = parts[1] if len(parts) > 1 else ""
        if key == "worktree":
            current["path"] = val
        elif key == "HEAD":
            current["head"] = val
        elif key == "branch":
            current["branch"] = val
        elif key == "locked":
            current["locked"] = True
    flush()
    return entries
