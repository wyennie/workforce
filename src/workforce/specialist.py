"""Specialist data model, on-disk store, and templates.

A specialist is a named persona Workforce can dispatch on a mission. Each
specialist has a role description, a base system prompt, an allowed tool set,
plus per-specialist memory (cross-project lessons) and stats (mission counts).

Storage layout:

    <roster_root>/<name>/
        specialist.toml   # Specialist model
        memory.md         # cross-project memory, append-only
        stats.json        # SpecialistStats model
"""

from __future__ import annotations

try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # Windows — file locking not available
import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from workforce import paths
from workforce.utils import _dump_toml

SCHEMA_VERSION = 1
DEFAULT_MODEL = "claude-sonnet-4-6"

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


# Tools available to a specialist by default. Mirrors the standard Claude Code
# tool surface; templates narrow this for restricted roles (e.g. reviewer).
ALL_DEV_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch"]


def common_preamble(name: str) -> str:
    """Common preamble baked into every template's base_prompt at hire time.

    Saved verbatim into the specialist's TOML — the user can edit it freely.
    Templates just seed sensible defaults.
    """
    return """\
You are operating inside a Workforce mission. The mission runner has placed
you in a git worktree on a fresh branch. Do the work, commit it, and finish.

## Commit policy

Commit your work as you go:
- After each meaningful unit of work — a feature subtask done, a test
  passing, a refactor isolated. Not after every file edit.
- Before any risky operation (large refactor, dependency change, destructive
  command) so the previous state is recoverable.
- Always before ending the mission. The branch must have no uncommitted
  changes when you finish.

Commit messages: conventional-commits style — `<type>(<scope>): <subject>`
where type is one of `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
Subject in imperative mood, under 72 chars. Body explains *why* when the
diff doesn't make it obvious.

Commits are authored as the repo's user (do not override `user.name` or
`user.email`).

## Working style

- Read before you write. Understand the existing code before modifying it.
- If the ticket is ambiguous and you cannot proceed safely, leave the branch
  in a committed state and explain in your final response what's missing.
- Tests count as meaningful work; add them when they would protect the
  change.
"""


@dataclass(frozen=True)
class Template:
    """Seed values for a new specialist hired from a template.

    Attributes:
        role: One-line role description baked into the specialist file.
        base_prompt: Full base_prompt that follows ``common_preamble``.
        allowed_tools: Tool names the specialist may invoke.
    """

    role: str
    base_prompt: str
    allowed_tools: list[str]


TEMPLATES: dict[str, Template] = {
    "backend": Template(
        role="Senior backend engineer. APIs, services, data models, and the boring infrastructure that holds them up.",
        base_prompt="""\
## Role

You are a senior backend engineer. You think in terms of contracts, failure
modes, and the long tail of operational pain. You favor boring, well-tested
solutions over clever ones.

When you change an API, consider compatibility. When you change a data
model, consider migrations. When you add a dependency, consider why the
stdlib won't do.
""",
        allowed_tools=ALL_DEV_TOOLS.copy(),
    ),
    "frontend": Template(
        role="Senior frontend engineer. Components, state, accessibility, and the user-visible surface.",
        base_prompt="""\
## Role

You are a senior frontend engineer. You build UIs that work for real users
on real devices. You care about accessibility, keyboard navigation, loading
states, and the things that break when the network is slow.

Match the existing codebase's conventions for state management, styling, and
component composition before introducing new patterns.
""",
        allowed_tools=ALL_DEV_TOOLS.copy(),
    ),
    "tester": Template(
        role="Test engineer. Writes and maintains tests; hunts regressions; raises coverage where it matters.",
        base_prompt="""\
## Role

You are a test engineer. You write tests that fail loudly when behavior
breaks and pass quietly when it doesn't. You target boundaries, edge cases,
and the integrations that everyone else avoids.

Prefer fewer good tests over many shallow ones. Prefer testing behavior over
testing implementation. Prefer real fakes over mocks when the cost is low.
""",
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
    ),
    "reviewer": Template(
        role="Code reviewer. Reads diffs, raises concerns, suggests changes. Does not modify code directly.",
        base_prompt="""\
## Role

You are a code reviewer. You read carefully, think about what could break,
and write reviews that respect the author's time. You may run read-only
commands (git log, tests, grep) to ground your review.

You do NOT modify code. You report findings. The mission's success criteria
should describe what you're reviewing and at what depth.
""",
        # No Write/Edit; Bash is allowed for read-only investigation (tests, git log).
        allowed_tools=["Read", "Bash", "Glob", "Grep"],
    ),
    "generalist": Template(
        role="Generalist engineer. Picks up whatever the ticket needs.",
        base_prompt="""\
## Role

You are a generalist engineer. You handle whatever the ticket needs —
backend, frontend, tests, plumbing, scripts. You match the codebase's style
rather than imposing your own.

When a ticket is bigger than it looks, finish a coherent slice, commit, and
note what's left for a follow-up.
""",
        allowed_tools=ALL_DEV_TOOLS.copy(),
    ),
}


class Specialist(BaseModel):
    """A persistent persona Workforce can dispatch on a mission."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    name: str
    role: str
    model: str = DEFAULT_MODEL
    allowed_tools: list[str] = Field(default_factory=lambda: ALL_DEV_TOOLS.copy())
    base_prompt: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not NAME_PATTERN.match(v):
            raise ValueError(
                "name must start with a lowercase letter and contain only "
                "lowercase letters, digits, '-' or '_' (max 32 chars)"
            )
        return v

    @classmethod
    def from_template(
        cls,
        name: str,
        template_name: str,
        *,
        role: str | None = None,
        model: str | None = None,
    ) -> Specialist:
        """Create a Specialist seeded from a built-in template.

        Args:
            name: Unique name for the new specialist (must match
                ``NAME_PATTERN``).
            template_name: Key in :data:`TEMPLATES` (e.g. ``"backend"``).
            role: Override for the template's default role description.
            model: Claude model id; defaults to :data:`DEFAULT_MODEL`.

        Returns:
            A fully initialized Specialist with the template's base_prompt
            prepended with :func:`common_preamble`.

        Raises:
            ValueError: If ``template_name`` isn't in :data:`TEMPLATES`.
        """
        if template_name not in TEMPLATES:
            raise ValueError(
                f"unknown template '{template_name}'; available: "
                f"{', '.join(sorted(TEMPLATES))}"
            )
        tmpl = TEMPLATES[template_name]
        return cls(
            name=name,
            role=role or tmpl.role,
            model=model or DEFAULT_MODEL,
            allowed_tools=tmpl.allowed_tools.copy(),
            base_prompt=common_preamble(name) + "\n" + tmpl.base_prompt,
        )

    @classmethod
    def custom(
        cls,
        name: str,
        *,
        role: str,
        model: str | None = None,
        allowed_tools: list[str] | None = None,
    ) -> Specialist:
        """Create a Specialist with a hand-written role (no template).

        The base_prompt is ``common_preamble`` + ``## Role\\n\\n<role>``.
        ``allowed_tools`` defaults to :data:`ALL_DEV_TOOLS` if omitted.

        Args:
            name: Unique specialist name.
            role: Free-text role description.
            model: Claude model id; defaults to :data:`DEFAULT_MODEL`.
            allowed_tools: Explicit tool list, or None to use ALL_DEV_TOOLS.

        Returns:
            A fully initialized Specialist.
        """
        return cls(
            name=name,
            role=role,
            model=model or DEFAULT_MODEL,
            allowed_tools=(allowed_tools if allowed_tools is not None else ALL_DEV_TOOLS).copy(),
            base_prompt=common_preamble(name) + "\n## Role\n\n" + role + "\n",
        )


class SpecialistStats(BaseModel):
    """Cumulative performance counters for one specialist, written to stats.json.

    Updated at the end of every mission (including failures and partial
    parallel runs). Provides a lightweight operational dashboard without
    scanning all mission meta files.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    missions_completed: int = 0
    missions_failed: int = 0
    total_cost_usd: float = 0.0
    total_duration_seconds: float = 0.0


class RosterError(Exception):
    """Raised for roster store errors that have a clear user-facing message."""


class RosterStore:
    """File-backed CRUD for specialists.

    Operations are intentionally simple — no caching, no in-memory index. Roster
    sizes are tiny (handfuls), so we re-read from disk on every call.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or paths.roster_dir()

    def _dir(self, name: str) -> Path:
        return self.root / name

    def _spec_path(self, name: str) -> Path:
        return self._dir(name) / "specialist.toml"

    def _stats_path(self, name: str) -> Path:
        return self._dir(name) / "stats.json"

    def _memory_path(self, name: str) -> Path:
        return self._dir(name) / "memory.md"

    def exists(self, name: str) -> bool:
        """Return True if ``<roster_root>/<name>/specialist.toml`` is a file."""
        return self._spec_path(name).is_file()

    def names(self) -> list[str]:
        """Return sorted list of all specialist names in the roster."""
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_dir() and (p / "specialist.toml").is_file()
        )

    def list(self) -> list[Specialist]:
        """Return all specialists in the roster, sorted by name."""
        return [self.load(n) for n in self.names()]

    def load(self, name: str) -> Specialist:
        """Load and validate a Specialist from its TOML file.

        Raises:
            RosterError: If the specialist doesn't exist.
        """
        path = self._spec_path(name)
        if not path.is_file():
            raise RosterError(f"no such specialist: {name!r}")
        with path.open("rb") as f:
            data = tomllib.load(f)
        return Specialist.model_validate(data)

    def save(self, spec: Specialist, *, overwrite: bool = False) -> None:
        """Persist a Specialist to ``specialist.toml``.

        Initializes empty ``stats.json`` and ``memory.md`` if they're absent
        (never overwrites existing stats/memory).

        Args:
            spec: The Specialist to save.
            overwrite: When False (default), raises if the name exists.

        Raises:
            RosterError: If the specialist already exists and overwrite is False.
        """
        if self.exists(spec.name) and not overwrite:
            raise RosterError(f"specialist {spec.name!r} already exists")
        d = self._dir(spec.name)
        d.mkdir(parents=True, exist_ok=True)
        self._spec_path(spec.name).write_text(
            _dump_toml(spec.model_dump(exclude_none=True))
        )
        # Initialize stats and memory if missing — never clobber.
        if not self._stats_path(spec.name).exists():
            self._write_stats(spec.name, SpecialistStats())
        if not self._memory_path(spec.name).exists():
            self._memory_path(spec.name).write_text("")

    def delete(self, name: str) -> None:
        """Remove a specialist's entire directory (including memory and stats).

        Raises:
            RosterError: If the specialist doesn't exist.
        """
        if not self.exists(name):
            raise RosterError(f"no such specialist: {name!r}")
        shutil.rmtree(self._dir(name))

    def load_stats(self, name: str) -> SpecialistStats:
        """Load stats for ``name``, returning a fresh SpecialistStats if absent."""
        path = self._stats_path(name)
        if not path.is_file():
            return SpecialistStats()
        return SpecialistStats.model_validate_json(path.read_text())

    def save_stats(self, name: str, stats: SpecialistStats) -> None:
        """Persist updated stats for ``name``.

        Raises:
            RosterError: If the specialist doesn't exist.
        """
        if not self.exists(name):
            raise RosterError(f"no such specialist: {name!r}")
        self._write_stats(name, stats)

    def _write_stats(self, name: str, stats: SpecialistStats) -> None:
        """Write stats to disk (no existence check — internal use only)."""
        self._stats_path(name).write_text(
            json.dumps(stats.model_dump(), indent=2) + "\n"
        )

    def load_memory(self, name: str) -> str:
        """Return the full cross-project memory text for ``name``.

        Returns an empty string if the memory file doesn't exist yet.

        Raises:
            RosterError: If the specialist doesn't exist.
        """
        if not self.exists(name):
            raise RosterError(f"no such specialist: {name!r}")
        path = self._memory_path(name)
        if not path.is_file():
            return ""
        return path.read_text()

    def append_memory(self, name: str, entry: str) -> None:
        """Append an entry to a specialist's cross-project memory.

        Uses an exclusive file lock so concurrent missions don't interleave
        writes. Entry is suffixed with a single trailing newline if missing.
        """
        if not self.exists(name):
            raise RosterError(f"no such specialist: {name!r}")
        if not entry.endswith("\n"):
            entry = entry + "\n"
        path = self._memory_path(name)
        with path.open("a") as f:
            if _fcntl is not None:
                # Unix: exclusive lock so concurrent missions don't interleave.
                _fcntl.flock(f.fileno(), _fcntl.LOCK_EX)
            try:
                f.write(entry)
            finally:
                if _fcntl is not None:
                    _fcntl.flock(f.fileno(), _fcntl.LOCK_UN)


