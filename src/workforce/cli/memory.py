"""CLI commands for managing specialist memory: compact, search, export, import, show."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from workforce import output, paths
from workforce.project import ProjectError, ProjectStore
from workforce.specialist import DEFAULT_MODEL, RosterError, RosterStore

sub = typer.Typer(
    name="memory",
    help="Inspect and manage specialist memory files.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _roster_store() -> RosterStore:
    """Ensure the Workforce data layout exists and return a RosterStore."""
    paths.ensure_layout()
    return RosterStore()


def _project_store() -> ProjectStore:
    """Ensure the Workforce data layout exists and return a ProjectStore."""
    paths.ensure_layout()
    return ProjectStore()


def _cross_project_memory_path(name: str) -> Path:
    """Return the cross-project memory file path for *name*."""
    return paths.roster_dir() / name / "memory.md"


def _project_memory_path(specialist: str, project_ref: str) -> Path:
    """Return the per-project memory file path for *specialist* in *project_ref*."""
    pstore = _project_store()
    try:
        proj = pstore.resolve(project_ref)
    except ProjectError as e:
        output.die(str(e))
    return pstore.memory_dir(proj.id) / f"{specialist}.md"


def _read_path(path: Path) -> str:
    """Read *path*, returning '' if it doesn't exist."""
    if not path.is_file():
        return ""
    return path.read_text()


def _write_path(path: Path, text: str) -> None:
    """Write *text* to *path*, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@sub.command("show")
def memory_show(
    specialist: str = typer.Argument(..., help="Specialist name."),
) -> None:
    """Print a summary table of memory files for a specialist.

    Shows file path, line count, and approximate token count (chars // 4)
    for both the cross-project memory and any per-project memory files
    that exist.
    """
    store = _roster_store()
    if not store.exists(specialist):
        output.die(f"no such specialist: {specialist!r}")

    table = Table(show_header=True, header_style="bold")
    table.add_column("scope")
    table.add_column("path", overflow="fold")
    table.add_column("lines", justify="right")
    table.add_column("~tokens", justify="right")

    cross_path = _cross_project_memory_path(specialist)
    cross_text = _read_path(cross_path)
    table.add_row(
        "cross-project",
        str(cross_path),
        str(len(cross_text.splitlines())),
        str(len(cross_text) // 4),
    )

    pstore = _project_store()
    for proj in pstore.list():
        if specialist in proj.assigned_specialists:
            mem_path = pstore.memory_dir(proj.id) / f"{specialist}.md"
            text = _read_path(mem_path)
            table.add_row(
                f"project: {proj.name}",
                str(mem_path),
                str(len(text.splitlines())),
                str(len(text) // 4),
            )

    output.print_table(table)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@sub.command("search")
def memory_search(
    specialist: str = typer.Argument(..., help="Specialist name."),
    query: str = typer.Argument(..., help="Search query (case-insensitive)."),
    project_ref: Optional[str] = typer.Option(
        None,
        "--project",
        metavar="PROJECT",
        help="Also search per-project memory for this project.",
    ),
) -> None:
    """Search specialist memory files for a query string.

    Greps both the cross-project memory and (if --project is given) the
    per-project memory file. Matching lines are printed with 2 lines of
    context, prefixed by which file they came from.
    """
    store = _roster_store()
    if not store.exists(specialist):
        output.die(f"no such specialist: {specialist!r}")

    files: list[tuple[str, Path]] = [
        ("cross-project", _cross_project_memory_path(specialist)),
    ]
    if project_ref is not None:
        pstore = _project_store()
        try:
            proj = pstore.resolve(project_ref)
        except ProjectError as e:
            output.die(str(e))
        files.append((
            f"project:{proj.name}",
            pstore.memory_dir(proj.id) / f"{specialist}.md",
        ))

    found_any = False
    pattern = re.compile(re.escape(query), re.IGNORECASE)

    for label, path in files:
        if not path.is_file():
            continue
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if not pattern.search(line):
                continue
            found_any = True
            # Print 2 lines of context before and after.
            start = max(0, i - 2)
            end = min(len(lines), i + 3)
            output.rule(f"[{label}]  line {i + 1}")
            for j in range(start, end):
                prefix = ">>> " if j == i else "    "
                output.info(f"{prefix}{lines[j]}")

    if not found_any:
        output.info(f"[dim]no matches for {query!r}[/dim]")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@sub.command("export")
def memory_export(
    specialist: str = typer.Argument(..., help="Specialist name."),
    project_ref: Optional[str] = typer.Option(
        None,
        "--project",
        metavar="PROJECT",
        help="Also export per-project memory for this project.",
    ),
) -> None:
    """Print specialist memory to stdout.

    Prints the cross-project memory with a '# Cross-project memory' header.
    If --project is given, also prints the project-scoped memory with a
    '# Project memory: <name>' header.
    """
    store = _roster_store()
    if not store.exists(specialist):
        output.die(f"no such specialist: {specialist!r}")

    cross_path = _cross_project_memory_path(specialist)
    cross_text = _read_path(cross_path)
    output.info("# Cross-project memory")
    output.info("")
    if cross_text.strip():
        output.info(cross_text.rstrip())
    else:
        output.info("[dim](empty)[/dim]")

    if project_ref is not None:
        pstore = _project_store()
        try:
            proj = pstore.resolve(project_ref)
        except ProjectError as e:
            output.die(str(e))
        mem_path = pstore.memory_dir(proj.id) / f"{specialist}.md"
        proj_text = _read_path(mem_path)
        output.info("")
        output.info(f"# Project memory: {proj.name}")
        output.info("")
        if proj_text.strip():
            output.info(proj_text.rstrip())
        else:
            output.info("[dim](empty)[/dim]")


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


@sub.command("import")
def memory_import(
    specialist: str = typer.Argument(..., help="Specialist name."),
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="Path to the file to import.",
        exists=True,
        file_okay=True,
        dir_okay=False,
    ),
    project_ref: Optional[str] = typer.Option(
        None,
        "--project",
        metavar="PROJECT",
        help="Import into the per-project memory for this project.",
    ),
    cross_project: bool = typer.Option(
        False,
        "--cross-project",
        help="Import into the cross-project (roster) memory. Default if --project omitted.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Replace a memory file with the contents of a local file.

    By default (or with --cross-project) replaces the cross-project memory.
    With --project, replaces the per-project memory for that project.
    Asks for confirmation showing old vs new byte count.
    """
    store = _roster_store()
    if not store.exists(specialist):
        output.die(f"no such specialist: {specialist!r}")

    new_text = file.read_text()

    if project_ref is not None and not cross_project:
        target_path = _project_memory_path(specialist, project_ref)
        scope = f"project memory ({project_ref})"
    else:
        target_path = _cross_project_memory_path(specialist)
        scope = "cross-project memory"

    old_text = _read_path(target_path)
    old_bytes = len(old_text.encode())
    new_bytes = len(new_text.encode())

    output.info(
        f"Importing into [bold]{scope}[/bold] for [bold]{specialist}[/bold]:"
    )
    output.info(f"  old size: {old_bytes} bytes")
    output.info(f"  new size: {new_bytes} bytes")
    output.info(f"  target:   {target_path}")

    if not yes:
        confirm = typer.confirm("Replace memory file?", default=False)
        if not confirm:
            output.info("aborted")
            raise typer.Exit()

    _write_path(target_path, new_text)
    output.success(f"imported {new_bytes} bytes into {scope} for {specialist!r}")


# ---------------------------------------------------------------------------
# compact
# ---------------------------------------------------------------------------

_COMPACT_SYSTEM_PROMPT = (
    "Summarize these specialist memory entries into a condensed form, "
    "preserving all unique facts. Output plain Markdown."
)


async def _run_compact_sdk(text: str, model: str) -> str:
    """Run a single-turn SDK session to compact *text*. Returns the response."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query as sdk_query,
    )

    options = ClaudeAgentOptions(
        cwd=str(Path.cwd()),
        model=model,
        max_turns=1,
        permission_mode="bypassPermissions",
        allowed_tools=[],
        system_prompt=_COMPACT_SYSTEM_PROMPT,
    )

    collected: list[str] = []

    async for msg in sdk_query(prompt=text, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    collected.append(block.text)
        elif isinstance(msg, ResultMessage):
            pass  # cost tracking not needed here

    return "\n".join(collected)


@sub.command("compact")
def memory_compact(
    specialist: str = typer.Argument(..., help="Specialist name."),
    project_ref: Optional[str] = typer.Option(
        None,
        "--project",
        metavar="PROJECT",
        help="Compact per-project memory for this project instead of cross-project.",
    ),
    keep_last: Optional[int] = typer.Option(
        None,
        "--keep-last",
        metavar="N",
        help="Preserve last N lines verbatim; compact only older content.",
    ),
    threshold_tokens: Optional[int] = typer.Option(
        None,
        "--threshold-tokens",
        metavar="N",
        help="Skip compaction if memory is under N tokens (chars // 4).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Compact a specialist's memory file using the AI.

    Feeds the memory file to the model with a summarisation prompt and writes
    the condensed result back. Asks for confirmation before overwriting.

    Use --threshold-tokens to skip the SDK call when the memory is already
    short. Use --keep-last N to preserve the most recent N lines exactly.
    """
    store = _roster_store()
    if not store.exists(specialist):
        output.die(f"no such specialist: {specialist!r}")

    if project_ref is not None:
        target_path = _project_memory_path(specialist, project_ref)
        scope = f"project memory ({project_ref})"
    else:
        target_path = _cross_project_memory_path(specialist)
        scope = "cross-project memory"

    old_text = _read_path(target_path)

    if not old_text.strip():
        output.info("Memory is empty, nothing to compact.")
        raise typer.Exit()

    approx_tokens = len(old_text) // 4
    if threshold_tokens is not None and approx_tokens < threshold_tokens:
        output.info(
            f"Memory is under threshold ({approx_tokens} < {threshold_tokens} tokens), skipping."
        )
        raise typer.Exit()

    # Split into tail (to keep verbatim) and head (to compact).
    tail_lines: list[str] = []
    head_text = old_text

    if keep_last is not None and keep_last > 0:
        all_lines = old_text.splitlines(keepends=True)
        if len(all_lines) > keep_last:
            tail_lines = all_lines[-keep_last:]
            head_text = "".join(all_lines[:-keep_last])
        else:
            output.info(
                f"Memory has {len(all_lines)} lines; --keep-last {keep_last} covers all "
                f"or more — nothing to compact."
            )
            raise typer.Exit()

    if not head_text.strip():
        output.info("No content to compact after reserving the last N lines.")
        raise typer.Exit()

    output.info(
        f"Compacting [bold]{scope}[/bold] for [bold]{specialist}[/bold] …"
    )

    try:
        spec = store.load(specialist)
        model = spec.model
    except RosterError:
        model = DEFAULT_MODEL

    try:
        compacted = asyncio.run(_run_compact_sdk(head_text, model))
    except Exception as e:
        output.die(f"SDK compaction failed: {e}")

    new_text = compacted
    if tail_lines:
        # Ensure compacted section ends with a newline before appending tail.
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += "".join(tail_lines)

    old_len = len(old_text)
    new_len = len(new_text)

    output.info(f"  old length: {old_len} chars (~{old_len // 4} tokens)")
    output.info(f"  new length: {new_len} chars (~{new_len // 4} tokens)")

    if not yes:
        confirm = typer.confirm(
            "Write compacted memory?", default=False
        )
        if not confirm:
            output.info("aborted")
            raise typer.Exit()

    _write_path(target_path, new_text)
    output.success(f"compacted {scope} for {specialist!r}")
