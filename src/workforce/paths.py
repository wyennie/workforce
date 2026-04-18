"""Filesystem layout for Workforce.

All on-disk state lives under WORKFORCE_HOME (default: ~/.workforce/).
Override with the WORKFORCE_HOME environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_VAR = "WORKFORCE_HOME"
_DEFAULT = "~/.workforce"


def home() -> Path:
    """Resolve WORKFORCE_HOME. Does not create the directory."""
    raw = os.environ.get(_ENV_VAR, _DEFAULT)
    return Path(raw).expanduser().resolve()


def config_path() -> Path:
    """Return the path to the global Workforce config file (may not exist yet)."""
    return home() / "config.toml"


def roster_dir() -> Path:
    """Return the directory that stores all specialist subdirectories."""
    return home() / "roster"


def specialist_dir(name: str) -> Path:
    """Return the directory for a single named specialist."""
    return roster_dir() / name


def projects_dir() -> Path:
    """Return the directory that stores all project subdirectories."""
    return home() / "projects"


def project_dir(project_id: str) -> Path:
    """Return the directory for a specific project by its id."""
    return projects_dir() / project_id


def ensure_layout() -> Path:
    """Create the base directory layout if missing. Returns home()."""
    h = home()
    (h / "roster").mkdir(parents=True, exist_ok=True)
    (h / "projects").mkdir(parents=True, exist_ok=True)
    return h
