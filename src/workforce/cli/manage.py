"""Interactive Manager chat session for a workforce project.

`workforce manage <project>` opens a Claude Agent SDK session bound to a
specific project, with a system prompt that teaches the Manager about the
workforce CLI. The user chats normally; the Manager dispatches workers via
`workforce dispatch ... --background` so each spawned mission runs detached;
output appears in a shared tail window the user keeps open in another terminal.

This is the Manager-as-a-conversation shape (vs the one-shot
`workforce dispatch` command): persistent context, multi-turn, the Manager
can answer questions about ongoing or past missions by inspecting the
project state on disk.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML

from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

from workforce import output, paths
from workforce.cli._common import _summarize_tool_args, _tool_color, _truncate
from workforce.project import Project
from workforce.specialist import RosterStore

# ----- system prompt --------------------------------------------------------


_MANAGER_PROMPT_TEMPLATE = """\
You are the Manager for the workforce project **{name}**.

You're in an interactive chat session with the user. Help them dispatch \
specialists to work on tasks, check on running missions, and review results. \
Carry context across turns — remember what's been discussed and what's running.

# Project state

- Project name: {name}
- Project id: {project_id}
- Kind: {kind}  ({kind_explanation})
- Working directory: {repo_path}
- Assigned specialists: {assigned}
- State on disk: {workforce_dir}{branch_state}

{roster_section}

# How to dispatch work

Use the workforce CLI via Bash. **Always pass `--background`** — never \
`--window`. The user already has ONE shared terminal window open running \
`workforce project tail {name}`, which streams output from every worker in \
this project, labeled by mission id and specialist. New missions appear in \
that window automatically as you dispatch them.

    workforce dispatch {name} "<ticket text>" --specialist <name> --background{branch_dispatch_suffix}

This returns immediately with a mission id and the worker runs detached. \
You don't see the output here; the user watches it in the shared tail \
window. After dispatch, continue the conversation; when the user asks how \
it's going or once you think a mission should be done, check status with \
`workforce mission show <id>`.

DO NOT use `--window` (would open a separate window per dispatch — the \
user explicitly asked for a single shared window). DO NOT omit \
`--background` (would block your conversation while the mission runs).{branch_dispatch_rule}

# Other commands you know

- `workforce mission show <mission_id>` — status, cost, result summary.
- `workforce missions {name}` — list all missions in this project.
- `workforce project show {name}` — assigned specialists, recorded missions.
- `workforce roster` — global roster of available specialists.
- `workforce hire <name> --role <role> --from-template <tmpl>` — add a new \
  specialist if the user wants someone the project doesn't have.
- `workforce project assign {name} <specialist>` — assign an existing \
  roster member to this project.

For reading mission outputs, use the Read tool against \
`{workforce_dir}/missions/<mission_id>/result.md` (or other artifacts \
inside that dir).

# Conventions

- **Confirm before expensive dispatches.** Before kicking off a long \
  mission, briefly tell the user what you'll dispatch ("I'll run scout on \
  ticket X") and let them say go. Cheap reads (status checks, listing \
  missions) don't need confirmation.
- **One ticket per dispatch.** If the user describes multiple pieces of \
  work, break them into separate dispatches with clear, narrow tickets.
- **Keep replies short.** The user is having a conversation, not reading \
  a report. Summarize, don't recite.
- **Use the user's specialists.** Don't invent specialist names — only \
  dispatch to those listed under "Assigned specialists" above. If the \
  user wants a new one, run `workforce hire`.
"""


_KIND_EXPLANATION = {
    "repo": (
        "git-tracked; missions run in worktrees and commit on their own branch"
    ),
    "workspace": (
        "plain working directory; missions edit files directly, no commits"
    ),
}


def _build_manager_prompt(
    project: Project,
    roster_store: RosterStore,
    *,
    branch: str | None = None,
) -> str:
    """Render the Manager system prompt for *project*.

    Injects project metadata, the specialist roster, and (when supplied)
    staging-branch dispatch rules so the Manager always knows the constraints
    it must operate under.
    """
    assigned = (
        ", ".join(project.assigned_specialists)
        if project.assigned_specialists else "(none yet — hire and assign one first)"
    )

    roster_lines: list[str] = []
    for name in project.assigned_specialists:
        if not roster_store.exists(name):
            roster_lines.append(f"- {name}: [missing from roster]")
            continue
        spec = roster_store.load(name)
        role = spec.role.split("\n", 1)[0].strip()
        roster_lines.append(f"- **{spec.name}** ({spec.model}) — {role}")
    roster_section = (
        "## Assigned specialists\n\n" + "\n".join(roster_lines)
        if roster_lines else
        "## Assigned specialists\n\n(none — ask the user to assign one before dispatching.)"
    )

    if branch:
        branch_state = (
            f"\n- **Staging branch: `{branch}`** "
            "— every dispatch must pass `--branch {branch}` so work forks "
            "from and merges back into this branch (main stays untouched)."
        ).format(branch=branch)
        branch_dispatch_suffix = f" --branch {branch}"
        branch_dispatch_rule = (
            "\n\n# Staging branch\n\n"
            f"This session is pinned to staging branch `{branch}`. **Every "
            f"`workforce dispatch` you run MUST include `--branch {branch}`.** "
            "That makes mission worktrees fork from the staging branch and "
            "auto-merge back into it on success. Do not omit it; do not pass "
            "a different branch."
        )
    else:
        branch_state = ""
        branch_dispatch_suffix = ""
        branch_dispatch_rule = ""

    return _MANAGER_PROMPT_TEMPLATE.format(
        name=project.name,
        project_id=project.id,
        kind=project.kind,
        kind_explanation=_KIND_EXPLANATION.get(project.kind, ""),
        repo_path=project.repo_path,
        assigned=assigned,
        workforce_dir=str(paths.project_dir(project.id)),
        roster_section=roster_section,
        branch_state=branch_state,
        branch_dispatch_suffix=branch_dispatch_suffix,
        branch_dispatch_rule=branch_dispatch_rule,
    )


# ----- banner ---------------------------------------------------------------


def _render_session_banner(
    project: Project,
    roster_store: RosterStore,
    *,
    branch: str | None = None,
) -> None:
    """Render the Rich Panel banner shown once at session start."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold dim", no_wrap=True)
    grid.add_column()

    grid.add_row(
        "project",
        f"[bold]{project.name}[/bold]  [dim]{project.id}[/dim]",
    )
    grid.add_row("kind", project.kind)
    grid.add_row("directory", str(project.repo_path))

    if project.assigned_specialists:
        specs_lines: list[str] = []
        for name in project.assigned_specialists:
            if roster_store.exists(name):
                sp = roster_store.load(name)
                role_short = sp.role.split("\n", 1)[0].strip()[:50]
                specs_lines.append(
                    f"[bold]{name}[/bold] [dim]({sp.model}) — {role_short}[/dim]"
                )
            else:
                specs_lines.append(f"[dim]{name} (missing)[/dim]")
        grid.add_row("specialists", "\n".join(specs_lines))
    else:
        grid.add_row("specialists", "[dim](none — hire and assign first)[/dim]")

    if branch:
        grid.add_row("branch", f"[bold cyan]{branch}[/bold cyan]")

    output.raw(Panel(
        grid,
        title="[bold cyan]workforce manage[/bold cyan]",
        title_align="left",
        border_style="cyan",
        padding=(0, 1),
    ))
    output.info(
        "[dim]  /exit or Ctrl-D to leave  ·  blank line submits multi-line drafts[/dim]"
    )


# ----- prompt styling -------------------------------------------------------

_PROMPT_MAIN = HTML("<ansigreen><b>you</b></ansigreen> <ansicyan>›</ansicyan> ")
_PROMPT_CONT = HTML("<ansicyan> ·</ansicyan> ")


async def _read_user_input(session: PromptSession[str]) -> str | None:
    """Read one user message via prompt_toolkit. Returns None on EOF or Ctrl-D.

    Multi-line input: keep reading until a blank line, so the user can paste
    a paragraph without the SDK seeing each line as a separate turn. A single
    `/exit` line exits.

    Using ``PromptSession.prompt_async()`` instead of the built-in ``input()``
    gives full readline-style editing, including correct backspace and cursor
    movement across visually-wrapped terminal lines.  The plain ``input()``
    call delegates to the terminal's raw line discipline which has no concept
    of visual line boundaries, so pressing backspace cannot cross a soft wrap.
    """
    lines: list[str] = []
    try:
        first = await session.prompt_async(_PROMPT_MAIN)
    except (EOFError, KeyboardInterrupt):
        return None
    if first.strip() in ("/exit", "/quit"):
        return None
    if not first.strip():
        return ""  # empty turn — skip
    lines.append(first)
    while True:
        try:
            nxt = await session.prompt_async(_PROMPT_CONT)
        except (EOFError, KeyboardInterrupt):
            break
        if not nxt.strip():
            break
        lines.append(nxt)
    return "\n".join(lines).strip()


# ----- spinner (thinking indicator) ----------------------------------------


class _SpinnerState:
    """Owns one transient Rich Live spinner; safe to start/stop multiple times.

    Guards itself against CI mode and non-TTY contexts so tests and headless
    runs don't see stray ANSI sequences.
    """

    def __init__(self) -> None:
        self._live: Live | None = None

    def _should_show(self) -> bool:
        return not output.is_ci_mode() and sys.stdout.isatty()

    def start(self) -> None:
        """Start the spinner if it is not already running."""
        if self._live is not None or not self._should_show():
            return
        spinner = Spinner(
            "dots",
            text=Text(" working…", style="dim"),
            style="cyan",
        )
        self._live = Live(
            spinner,
            console=output._stdout,
            transient=True,
            refresh_per_second=12,
        )
        self._live.start()

    def stop(self) -> None:
        """Stop and erase the spinner if it is running."""
        if self._live is None:
            return
        self._live.stop()
        self._live = None


# ----- message rendering ----------------------------------------------------


def _extract_tool_result_text(block: ToolResultBlock) -> str:
    """Extract plain text from a ToolResultBlock's content."""
    if isinstance(block.content, str):
        return block.content
    if isinstance(block.content, list):
        return " ".join(getattr(b, "text", "") for b in block.content)
    return ""


def _render_tool_call(name: str, args: dict[str, Any]) -> None:
    """Print a color-coded one-line tool call preview."""
    color = _tool_color(name)
    preview = _truncate(_summarize_tool_args(name, args), 72)
    output.info(f"  [{color}]→ {name}[/{color}][dim]  {preview}[/dim]")


def _render_message(msg: Any, *, _state: dict[str, Any]) -> None:
    """Render an SDK message to the chat terminal.

    Args:
        msg: Any SDK message type (AssistantMessage, UserMessage, etc.).
        _state: Mutable state dict shared across calls within one render_loop.
            Keys used: ``header_printed`` (bool), ``total_cost`` (float),
            ``total_turns`` (int).
    """
    if isinstance(msg, AssistantMessage):
        if not _state.get("header_printed"):
            output.info("\n[bold green]●[/bold green] [bold]manager[/bold]")
            _state["header_printed"] = True
        for block in msg.content:
            if isinstance(block, TextBlock) and block.text.strip():
                output.raw(Markdown(block.text.rstrip(), code_theme="monokai"))
            elif isinstance(block, ToolUseBlock):
                _render_tool_call(block.name, block.input)
            elif isinstance(block, ThinkingBlock):
                # Don't echo thinking — too noisy for chat. The events log
                # has it for replay.
                pass

    elif isinstance(msg, UserMessage):
        # UserMessage.content is `str | list[ContentBlock]` — only iterate
        # the list form; bare strings are user input we don't need to echo.
        if isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, ToolResultBlock) and block.is_error:
                    text = _extract_tool_result_text(block)
                    output.warn(
                        f"  [red]✗[/red] [dim]tool error:[/dim] {text[:200]}"
                    )

    elif isinstance(msg, SystemMessage):
        # Permission prompts surface here — show them clearly.
        if getattr(msg, "subtype", "") == "permission_request":
            output.raw(Panel(
                str(msg),
                title="[bold yellow]⚠ permission needed[/bold yellow]",
                title_align="left",
                border_style="yellow",
                padding=(0, 1),
            ))

    elif isinstance(msg, ResultMessage):
        # Accumulate cost/turns for session-end display.
        _state["total_cost"] = _state.get("total_cost", 0.0) + (
            msg.total_cost_usd or 0.0
        )
        _state["total_turns"] = _state.get("total_turns", 0) + (
            msg.num_turns or 0
        )
        # Dim rule closes the turn visually.
        output.rule(style="dim")
        _state["header_printed"] = False


def _summarize_tool(name: str, args: dict[str, Any]) -> str:
    """Return a compact one-line preview of a tool call for the chat display.

    Kept for backward compatibility with existing tests. New rendering code
    uses :func:`_render_tool_call` instead.
    """
    if name == "Bash":
        cmd = str(args.get("command", ""))
        return cmd if len(cmd) <= 80 else cmd[:77] + "..."
    if name in ("Read", "Write", "Edit", "MultiEdit"):
        return str(args.get("file_path", ""))
    if name in ("Glob", "Grep"):
        return str(args.get("pattern", ""))
    # Fallback: first arg key=value
    if args:
        k, v = next(iter(args.items()))
        return f"{k}={v}"
    return ""


async def run_manager_chat(
    project: Project,
    roster_store: RosterStore,
    *,
    yolo: bool = False,
    branch: str | None = None,
) -> int:
    """Open the chat session. Returns exit code (0 on clean exit)."""
    system_prompt = _build_manager_prompt(project, roster_store, branch=branch)

    options = ClaudeAgentOptions(
        cwd=str(project.repo_path),
        system_prompt=system_prompt,
        model=project.default_model or "claude-sonnet-4-6",
        permission_mode="bypassPermissions" if yolo else "default",
        allowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
    )

    _render_session_banner(project, roster_store, branch=branch)

    # Pop up the single shared tail window. Every worker the Manager dispatches
    # via `--background` lands in here automatically, labeled by mission id +
    # specialist. Failure to spawn (headless server, no terminal installed)
    # is non-fatal — the chat still works; the user just won't see live
    # worker output.
    from workforce.terminal import open_terminal_window
    spawned = open_terminal_window(
        title=f"workforce: {project.name} (workers)",
        command=["workforce", "project", "tail", project.name],
    )
    if spawned:
        output.info(
            "[dim]opened a shared tail window — worker output will stream there.[/dim]"
        )
    else:
        output.warn(
            "[dim]could not open a tail window. Run "
            f"`workforce project tail {project.name}` in another terminal "
            "to watch workers.[/dim]"
        )

    # Pipeline: input_loop reads user lines and pushes onto pending_inputs.
    # feed() yields them as SDK message dicts. render_loop consumes SDK
    # responses and signals turn_done so input_loop can re-prompt.
    #
    # Terminal ownership:
    #   AI phase  — Rich (output.*, spinner Live) owns the terminal.
    #   Input phase — prompt_toolkit owns the terminal.
    # Rich Live MUST be stopped before prompt_async() is called. The
    # turn_done event and _spinner.stop() call in input_loop enforce this.
    pending_inputs: asyncio.Queue[str | None] = asyncio.Queue()
    turn_done = asyncio.Event()
    turn_done.set()  # ready for first user input
    stop = asyncio.Event()
    _session: PromptSession[str] = PromptSession()
    _spinner = _SpinnerState()

    async def feed() -> AsyncIterator[dict[str, Any]]:
        while True:
            text = await pending_inputs.get()
            if text is None:
                return
            turn_done.clear()
            yield {
                "type": "user",
                "session_id": "",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
            }

    async def render_loop() -> None:
        _state: dict[str, Any] = {
            "header_printed": False,
            "total_cost": 0.0,
            "total_turns": 0,
        }
        try:
            first_message = True
            async for msg in query(prompt=feed(), options=options):
                if first_message:
                    # Stop spinner on first real SDK message — transient=True
                    # erases the spinner line cleanly before response text.
                    _spinner.stop()
                    first_message = False
                _render_message(msg, _state=_state)
                if isinstance(msg, ResultMessage):
                    turn_done.set()
        except Exception as e:  # SDK or transport failure
            _spinner.stop()
            output.raw(Panel(
                f"[red]{type(e).__name__}[/red]: {e}",
                title="[bold red]✗ session error[/bold red]",
                title_align="left",
                border_style="red",
                padding=(0, 1),
            ))
            turn_done.set()   # unblock input_loop so gather() can exit
            stop.set()

        # Session-end rule with accumulated cost/turns.
        total_turns = _state.get("total_turns", 0)
        total_cost = _state.get("total_cost", 0.0)
        if total_turns > 0:
            output.rule(
                f"[dim]session ended — {total_turns} turns  ${total_cost:.4f}[/dim]",
                style="dim",
            )
        else:
            output.rule("[dim]session ended[/dim]", style="dim")

        # Signal input_loop to exit.  Without this, when the SDK session ends
        # normally (generator exhausts without error), input_loop hangs forever
        # waiting on turn_done and gather() never resolves.
        stop.set()
        await pending_inputs.put(None)   # unblocks feed() so gather() can exit

    async def input_loop() -> None:
        while not stop.is_set():
            await turn_done.wait()
            if stop.is_set():
                break
            # Rich Live must be stopped before prompt_async — spinner was already
            # stopped in render_loop on first message; this stop() is a no-op
            # for the normal path but guards against any edge cases.
            _spinner.stop()
            text = await _read_user_input(_session)
            if text is None:
                # EOF or /exit
                await pending_inputs.put(None)
                stop.set()
                break
            if not text.strip():
                continue  # skip empty turn, re-prompt
            # Start spinner before submitting so it's visible during AI generation.
            _spinner.start()
            await pending_inputs.put(text)

    try:
        await asyncio.gather(render_loop(), input_loop())
    except KeyboardInterrupt:
        output.info("[dim](interrupted)[/dim]")
    return 0


def manage_command_main(
    project: Project,
    roster_store: RosterStore,
    *,
    yolo: bool,
    branch: str | None = None,
) -> int:
    """Entry point used by the CLI; wraps the async session in asyncio.run."""
    if branch is not None:
        if project.kind == "workspace":
            output.fail(
                "--branch is only meaningful for repo-kind projects "
                "(workspaces have no branches)."
            )
            return 1
        from workforce.worktree import WorktreeError, ensure_branch
        try:
            ensure_branch(Path(project.repo_path), branch)
        except WorktreeError as e:
            output.fail(str(e))
            return 1
        output.info(
            f"[dim]session pinned to staging branch [bold]{branch}[/bold] — "
            "every dispatch will fork from and merge back into it.[/dim]"
        )
    try:
        return asyncio.run(run_manager_chat(project, roster_store, yolo=yolo, branch=branch))
    except KeyboardInterrupt:
        return 130


__all__ = ["run_manager_chat", "manage_command_main"]
