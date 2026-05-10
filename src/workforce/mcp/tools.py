"""Pure-Python implementations of the four workforce MCP tools.

Each function is dependency-free from the MCP SDK so it can be tested without
the optional ``mcp`` package installed.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast


def workforce_dispatch(
    project: str,
    ticket: str,
    specialist: str | None = None,
    auto_merge: bool = False,
) -> dict[str, Any]:
    """Dispatch a mission and return the CI result dict.

    Args:
        project: Project name or ID.
        ticket: Task description or ticket text.
        specialist: Optional specialist name.  Auto-selected when omitted.
        auto_merge: Merge the branch automatically on completion.

    Returns:
        Parsed JSON result dict on success, or ``{'error': stderr}`` on failure.
    """
    # Write ticket to a tempfile to avoid OS arg-length limits on long tickets.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", prefix="wf-ticket-", delete=False
    ) as tf:
        tf.write(ticket)
        ticket_path = Path(tf.name)

    try:
        cmd = ["workforce", "dispatch", project, "--file", str(ticket_path), "--ci"]
        if specialist:
            cmd += ["--specialist", specialist]
        if auto_merge:
            cmd += ["--auto-merge"]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1920
        )
    finally:
        ticket_path.unlink(missing_ok=True)

    if result.returncode == 0:
        return cast(dict[str, Any], json.loads(result.stdout))
    return {"error": result.stderr}


def workforce_mission_status(mission_id: str) -> dict[str, Any]:
    """Return the meta.json dict for *mission_id*, or an error dict.

    Args:
        mission_id: Mission ID as returned by ``workforce_dispatch``.

    Returns:
        Parsed meta.json dict, or ``{'error': 'mission <id> not found'}``.
    """
    from workforce import mission as m
    from workforce.project import ProjectStore

    store = ProjectStore()
    for proj in store.list():
        meta_path = m.mission_paths(proj.id, mission_id).meta
        if meta_path.exists():
            return cast(dict[str, Any], json.loads(meta_path.read_text()))
    return {"error": f"mission {mission_id} not found"}


def workforce_roster() -> list[dict[str, Any]]:
    """Return a list of specialist summary dicts.

    Each dict has keys: ``name``, ``role``, ``missions`` (total runs),
    ``cost_usd`` (cumulative).

    Returns:
        List of dicts, one per specialist in the roster.
    """
    from workforce.specialist import RosterStore

    store = RosterStore()
    result = []
    for name in store.names():
        spec = store.load(name)
        stats = store.load_stats(name)
        result.append(
            {
                "name": name,
                "role": spec.role,
                "missions": stats.missions_completed + stats.missions_failed,
                "cost_usd": stats.total_cost_usd,
            }
        )
    return result


def workforce_mission_result(mission_id: str) -> str:
    """Return the result.md text for *mission_id*, or an error message.

    Args:
        mission_id: Mission ID as returned by ``workforce_dispatch``.

    Returns:
        Raw Markdown text of result.md, or an error string when not found.
    """
    from workforce import mission as m
    from workforce.project import ProjectStore

    store = ProjectStore()
    for proj in store.list():
        result_path = m.mission_paths(proj.id, mission_id).root / "result.md"
        if result_path.exists():
            return result_path.read_text()
    return f"No result found for mission {mission_id}"
