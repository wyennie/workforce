"""Shell completion callbacks for Typer CLI arguments.

Each function follows the Typer/Click autocompletion protocol:
    (ctx, param, incomplete) -> list[str]

They are deliberately defensive — any failure returns an empty list so that
tab-completion degrades gracefully rather than printing a traceback.
"""

from __future__ import annotations

from typing import Any


def complete_project(ctx: Any, args: Any, incomplete: str) -> list[str]:
    """Return project names that start with *incomplete*."""
    try:
        from workforce.project import ProjectStore
        store = ProjectStore()
        return [p.name for p in store.list() if p.name.startswith(incomplete)]
    except Exception:
        return []


def complete_specialist(ctx: Any, args: Any, incomplete: str) -> list[str]:
    """Return specialist names that start with *incomplete*."""
    try:
        from workforce.specialist import RosterStore
        store = RosterStore()
        return [n for n in store.names() if n.startswith(incomplete)]
    except Exception:
        return []


def complete_mission_id(ctx: Any, args: Any, incomplete: str) -> list[str]:
    """Return mission ids (across all projects) that start with *incomplete*."""
    try:
        from workforce.project import ProjectStore
        store = ProjectStore()
        ids: list[str] = []
        for proj in store.list():
            mp_root = store.missions_dir(proj.id)
            if mp_root.exists():
                ids += [
                    d.name
                    for d in mp_root.iterdir()
                    if d.is_dir() and d.name.startswith(incomplete)
                ]
        return ids
    except Exception:
        return []
