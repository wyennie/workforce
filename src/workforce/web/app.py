"""FastAPI web dashboard for Workforce.

Requires the [web] optional extras:
    pip install 'workforce[web]'
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, StreamingResponse
    from fastapi.templating import Jinja2Templates
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "workforce[web] extras required: pip install 'workforce[web]'"
    ) from exc

from workforce import paths
from workforce.mission import MissionMeta, mission_paths
from workforce.project import ProjectStore
from workforce.specialist import RosterStore

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

app = FastAPI(title="Workforce Dashboard")


# ── helpers ──────────────────────────────────────────────────────────────────


def _load_all_missions() -> list[MissionMeta]:
    """Scan all projects and load every mission meta.json.

    Skips parallel parent metas (those with a ``parent_mission_id`` key) and
    any files that fail to parse.  Returns missions sorted newest-first by
    ``started_at``.
    """
    projects = paths.projects_dir()
    metas: list[MissionMeta] = []
    if not projects.is_dir():
        return metas
    for proj_dir in sorted(projects.iterdir()):
        if not proj_dir.is_dir():
            continue
        missions_dir = proj_dir / "missions"
        if not missions_dir.is_dir():
            continue
        for mission_dir in sorted(missions_dir.iterdir()):
            if not mission_dir.is_dir():
                continue
            meta_path = mission_dir / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                if "parent_mission_id" in data:
                    continue
                metas.append(MissionMeta.model_validate(data))
            except Exception:
                pass
    return sorted(metas, key=lambda m: m.started_at, reverse=True)


def _status_badge(status: str) -> str:
    """Return an HTML badge span for a mission status string."""
    ok = {"completed"}
    warn = {"review_rejected", "interrupted"}
    cls = "badge-ok" if status in ok else ("badge-warn" if status in warn else "badge-err")
    return f'<span class="badge {cls}">{status}</span>'


def _fmt_duration(secs: float) -> str:
    """Format a duration in seconds as ``Xm YYs`` or ``Xs``."""
    s = int(secs)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m{s % 60:02d}s"


def _fmt_cost(cost: float) -> str:
    """Format a USD cost to 4 decimal places."""
    return f"${cost:.4f}"


def _highlight_diff(diff_text: str) -> list[tuple[str, str]]:
    """Return ``(css_class, line)`` pairs for a unified diff string.

    Addition lines get ``diff-add``, removal lines get ``diff-del``, and
    everything else (headers, context) gets ``diff-ctx``.
    """
    rows: list[tuple[str, str]] = []
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            rows.append(("diff-add", line))
        elif line.startswith("-") and not line.startswith("---"):
            rows.append(("diff-del", line))
        else:
            rows.append(("diff-ctx", line))
    return rows


# ── routes ───────────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def mission_list(
    request: Request,
    project: str = "",
    status: str = "",
    since: str = "",
) -> HTMLResponse:
    """Mission list page with optional filters."""
    all_missions = _load_all_missions()

    missions = all_missions
    if project:
        missions = [m for m in missions if m.project_name.lower() == project.lower()]
    if status:
        missions = [m for m in missions if m.status == status]
    if since:
        try:
            since_date = date.fromisoformat(since)
            missions = [
                m for m in missions
                if date.fromisoformat(m.started_at[:10]) >= since_date
            ]
        except ValueError:
            pass

    project_names = sorted({m.project_name for m in all_missions})

    return templates.TemplateResponse(
        request,
        "missions.html",
        {
            "missions": missions,
            "project_names": project_names,
            "filter_project": project,
            "filter_status": status,
            "filter_since": since,
            "status_badge": _status_badge,
            "fmt_duration": _fmt_duration,
            "fmt_cost": _fmt_cost,
        },
    )


@app.get("/mission/{mission_id}", response_class=HTMLResponse)
async def mission_detail(request: Request, mission_id: str) -> HTMLResponse:
    """Mission detail page showing ticket, result, and meta fields."""
    all_missions = _load_all_missions()
    meta = next((m for m in all_missions if m.mission_id == mission_id), None)
    if meta is None:
        return HTMLResponse("<h1>Mission not found</h1>", status_code=404)

    mp = mission_paths(meta.project_id, mission_id)
    ticket = mp.ticket.read_text(encoding="utf-8") if mp.ticket.is_file() else "(no ticket)"
    result = mp.result.read_text(encoding="utf-8") if mp.result.is_file() else "(no result)"

    return templates.TemplateResponse(
        request,
        "mission_detail.html",
        {
            "meta": meta,
            "ticket": ticket,
            "result": result,
            "fmt_duration": _fmt_duration,
            "fmt_cost": _fmt_cost,
        },
    )


@app.get("/mission/{mission_id}/diff", response_class=HTMLResponse)
async def mission_diff(request: Request, mission_id: str) -> HTMLResponse:
    """Diff viewer: runs ``git diff {base_sha}..HEAD`` in the mission worktree."""
    all_missions = _load_all_missions()
    meta = next((m for m in all_missions if m.mission_id == mission_id), None)
    if meta is None:
        return HTMLResponse("<h1>Mission not found</h1>", status_code=404)

    diff_lines: list[tuple[str, str]] = []
    error_msg = ""

    if meta.worktree_path and meta.base_sha:
        worktree = Path(meta.worktree_path)
        if worktree.is_dir():
            try:
                result = subprocess.run(
                    ["git", "diff", f"{meta.base_sha}..HEAD"],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if result.returncode == 0:
                    diff_lines = _highlight_diff(result.stdout)
                else:
                    error_msg = result.stderr[:500] or "git diff failed with no stderr"
            except subprocess.TimeoutExpired:
                error_msg = "git diff timed out after 30 seconds"
            except FileNotFoundError:
                error_msg = "git not found in PATH"
        else:
            error_msg = f"worktree path not found: {meta.worktree_path}"
    else:
        error_msg = (
            "No worktree or base SHA available — "
            "this is a workspace project or the mission is still running."
        )

    return templates.TemplateResponse(
        request,
        "mission_diff.html",
        {
            "meta": meta,
            "diff_lines": diff_lines,
            "error_msg": error_msg,
        },
    )


def _is_result_message(data: dict[str, Any]) -> bool:
    """Detect a ResultMessage record in the events JSONL stream.

    The runner tags every record with ``_type`` = the SDK class name, so a
    ResultMessage will have ``_type == "ResultMessage"``.
    """
    return data.get("_type") == "ResultMessage"


async def _sse_generator(mission_id: str):  # type: ignore[return]
    """Async generator yielding SSE-formatted lines from the events.jsonl file.

    Scans all project mission directories to locate the events file.  Streams
    existing lines immediately, then polls for new lines every 0.5 s while the
    mission is still running (meta.json absent).  Closes when a ResultMessage
    is seen or the mission terminates.
    """
    # Locate the events file without requiring meta.json (mission may be live).
    events_path: Path | None = None
    projects = paths.projects_dir()
    if projects.is_dir():
        for proj_dir in projects.iterdir():
            if not proj_dir.is_dir():
                continue
            candidate = proj_dir / "missions" / mission_id / "events.jsonl"
            if candidate.parent.is_dir():
                events_path = candidate
                break

    if events_path is None:
        yield 'data: {"error": "mission not found"}\n\n'
        return

    meta_path = events_path.parent / "meta.json"
    lines_sent = 0

    try:
        while True:
            is_running = not meta_path.is_file()

            if events_path.is_file():
                text = events_path.read_text(encoding="utf-8", errors="replace")
                all_lines = text.splitlines()
                for line in all_lines[lines_sent:]:
                    line = line.strip()
                    if not line:
                        continue
                    yield f"data: {line}\n\n"
                    lines_sent += 1
                    try:
                        if _is_result_message(json.loads(line)):
                            return
                    except json.JSONDecodeError:
                        pass

            if not is_running:
                break
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass


@app.get("/mission/{mission_id}/events")
async def mission_events(mission_id: str) -> StreamingResponse:
    """Server-Sent Events stream for a mission's events.jsonl."""
    return StreamingResponse(
        _sse_generator(mission_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request) -> HTMLResponse:
    """Stats page: cost by specialist, missions per day, success rates."""
    missions = _load_all_missions()

    cost_by_spec: dict[str, float] = {}
    count_by_spec: dict[str, int] = {}
    done_by_spec: dict[str, int] = {}

    for m in missions:
        cost_by_spec[m.specialist] = cost_by_spec.get(m.specialist, 0.0) + m.cost_usd
        count_by_spec[m.specialist] = count_by_spec.get(m.specialist, 0) + 1
        if m.status == "completed":
            done_by_spec[m.specialist] = done_by_spec.get(m.specialist, 0) + 1

    specialist_stats = []
    for name in sorted(cost_by_spec):
        total = count_by_spec[name]
        done = done_by_spec.get(name, 0)
        rate = f"{100 * done // total}%" if total else "—"
        specialist_stats.append(
            {"name": name, "cost": cost_by_spec[name], "missions": total, "success_rate": rate}
        )

    today = date.today()
    day_counts: dict[str, int] = {}
    for m in missions:
        try:
            d = m.started_at[:10]
            if (today - date.fromisoformat(d)).days <= 30:
                day_counts[d] = day_counts.get(d, 0) + 1
        except (ValueError, IndexError):
            pass

    last_30 = [
        {"date": (today - timedelta(days=i)).isoformat(), "count": 0}
        for i in range(30, -1, -1)
    ]
    for entry in last_30:
        entry["count"] = day_counts.get(entry["date"], 0)

    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "specialist_stats": specialist_stats,
            "last_30": last_30,
            "total_cost": sum(m.cost_usd for m in missions),
            "total_missions": len(missions),
            "fmt_cost": _fmt_cost,
        },
    )


@app.get("/roster", response_class=HTMLResponse)
async def roster_page(request: Request) -> HTMLResponse:
    """Roster page: all specialists with mission counts and memory size."""
    rstore = RosterStore()
    all_missions = _load_all_missions()

    mission_count: dict[str, int] = {}
    mission_cost: dict[str, float] = {}
    for m in all_missions:
        mission_count[m.specialist] = mission_count.get(m.specialist, 0) + 1
        mission_cost[m.specialist] = mission_cost.get(m.specialist, 0.0) + m.cost_usd

    specialists = []
    for spec in rstore.list():
        stats = rstore.load_stats(spec.name)
        memory_path = rstore.root / spec.name / "memory.md"
        memory_bytes = memory_path.stat().st_size if memory_path.is_file() else 0
        specialists.append(
            {
                "spec": spec,
                "stats": stats,
                "missions": mission_count.get(spec.name, 0),
                "cost": mission_cost.get(spec.name, 0.0),
                "memory_bytes": memory_bytes,
            }
        )

    return templates.TemplateResponse(
        request,
        "roster.html",
        {"specialists": specialists, "fmt_cost": _fmt_cost},
    )
