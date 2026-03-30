"""Interactive Manager chat session for a workforce project.

`workforce manage <project>` opens a Claude Agent SDK session bound to a
specific project, with a system prompt that teaches the Manager about the
workforce CLI. The user chats normally; the Manager dispatches workers via
`workforce dispatch ... --window` so each spawned mission opens its own
terminal window the user can watch.

This is the Manager-as-a-conversation shape (vs the one-shot
`workforce dispatch` command): persistent context, multi-turn, the Manager
can answer questions about ongoing or past missions by inspecting the
project state on disk.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

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
- State on disk: {workforce_dir}

{roster_section}

# How to dispatch work

Use the workforce CLI via Bash. **Always pass `--background`** — never \
`--window`. The user already has ONE shared terminal window open running \
`workforce project tail {name}`, which streams output from every worker in \
this project, labeled by mission id and specialist. New missions appear in \
that window automatically as you dispatch them.

    workforce dispatch {name} "<ticket text>" --specialist <name> --background

This returns immediately with a mission id and the worker runs detached. \
You don't see the output here; the user watches it in the shared tail \
window. After dispatch, continue the conversation; when the user asks how \
it's going or once you think a mission should be done, check status with \
`workforce mission show <id>`.

DO NOT use `--window` (would open a separate window per dispatch — the \
user explicitly asked for a single shared window). DO NOT omit \
`--background` (would block your conversation while the mission runs).

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


def _build_manager_prompt(project: Project, roster_store: RosterStore) -> str:
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

    return _MANAGER_PROMPT_TEMPLATE.format(
        name=project.name,
        project_id=project.id,
        kind=project.kind,
        kind_explanation=_KIND_EXPLANATION.get(project.kind, ""),
        repo_path=project.repo_path,
        assigned=assigned,
        workforce_dir=str(paths.project_dir(project.id)),
        roster_section=roster_section,
    )


# ----- chat session ---------------------------------------------------------


_CHAT_BANNER = """\
[bold]workforce manage[/bold] — interactive Manager session for [bold]{name}[/bold]
[dim]project: {project_id}  ({kind})  cwd: {repo_path}[/dim]
[dim]commands: empty line submits a multi-line draft; type [bold]/exit[/bold] or Ctrl-D to leave.[/dim]
"""


def _read_user_input() -> str | None:
    """Read one user message from stdin. Returns None on EOF (Ctrl-D).

    Multi-line input: keep reading until a blank line, so the user can paste
    a paragraph without the SDK seeing each line as a separate turn. A single
    `/exit` line exits.
    """
    lines: list[str] = []
    try:
        first = input("manager> ")
    except EOFError:
        return None
    if first.strip() in ("/exit", "/quit"):
        return None
    if not first.strip():
        return ""  # empty turn — skip
    lines.append(first)
    while True:
        try:
            nxt = input("        ")
        except EOFError:
            break
        if not nxt.strip():
            break
        lines.append(nxt)
    return "\n".join(lines).strip()


def _render_message(msg: Any) -> None:
    """Render an SDK message to the chat terminal.

    Mirrors Claude Code's chat feel: assistant text streams; tool calls show
    a one-line note so the user knows when the Manager is doing something.
    """
    if isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock) and block.text.strip():
                output.raw(block.text.rstrip())
            elif isinstance(block, ThinkingBlock):
                # Don't echo thinking — too noisy for chat. The events log
                # has it for replay.
                pass
            elif isinstance(block, ToolUseBlock):
                summary = _summarize_tool(block.name, block.input)
                output.info(f"[dim]→ {block.name}({summary})[/dim]")
    elif isinstance(msg, UserMessage):
        # UserMessage.content is `str | list[ContentBlock]` — only iterate
        # the list form; bare strings are user input we don't need to echo.
        if isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, ToolResultBlock) and block.is_error:
                    # Only flag failures — successes would drown the chat.
                    text = ""
                    if isinstance(block.content, str):
                        text = block.content
                    elif isinstance(block.content, list):
                        text = " ".join(
                            getattr(b, "text", "") for b in block.content
                        )
                    output.warn(f"[dim]✗ tool error: {text[:200]}[/dim]")
    elif isinstance(msg, SystemMessage):
        # Permission prompts surface here — show them.
        if getattr(msg, "subtype", "") == "permission_request":
            output.warn(f"[bold]permission needed:[/bold] {msg}")
    elif isinstance(msg, ResultMessage):
        # End-of-turn marker; don't print, but caller uses this to re-prompt.
        pass


def _summarize_tool(name: str, args: dict[str, Any]) -> str:
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
) -> int:
    """Open the chat session. Returns exit code (0 on clean exit)."""
    system_prompt = _build_manager_prompt(project, roster_store)

    options = ClaudeAgentOptions(
        cwd=str(project.repo_path),
        system_prompt=system_prompt,
        model=project.default_model or "claude-sonnet-4-6",
        permission_mode="bypassPermissions" if yolo else "default",
        allowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
    )

    output.raw(_CHAT_BANNER.format(
        name=project.name, project_id=project.id, kind=project.kind,
        repo_path=project.repo_path,
    ))

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
    pending_inputs: asyncio.Queue[str | None] = asyncio.Queue()
    turn_done = asyncio.Event()
    turn_done.set()  # ready for first user input
    stop = asyncio.Event()

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
        try:
            async for msg in query(prompt=feed(), options=options):
                _render_message(msg)
                if isinstance(msg, ResultMessage):
                    turn_done.set()
        except Exception as e:  # SDK or transport failure
            output.fail(f"chat session ended: {type(e).__name__}: {e}")
            stop.set()

    async def input_loop() -> None:
        while not stop.is_set():
            await turn_done.wait()
            if stop.is_set():
                break
            text = await asyncio.to_thread(_read_user_input)
            if text is None:
                # EOF or /exit
                await pending_inputs.put(None)
                stop.set()
                break
            if not text.strip():
                continue  # skip empty turn, re-prompt
            await pending_inputs.put(text)

    try:
        await asyncio.gather(render_loop(), input_loop())
    except KeyboardInterrupt:
        output.info("[dim](interrupted)[/dim]")
    output.info("[dim]session ended.[/dim]")
    return 0


def manage_command_main(project: Project, roster_store: RosterStore, *, yolo: bool) -> int:
    """Entry point used by the CLI; wraps the async session in asyncio.run."""
    try:
        return asyncio.run(run_manager_chat(project, roster_store, yolo=yolo))
    except KeyboardInterrupt:
        return 130


__all__ = ["run_manager_chat", "manage_command_main"]
