"""Throwaway: validate claude-agent-sdk's actual streaming shape.

NOT shipped. Lives here so we have a working reference for what messages and
events look like before we build the real mission runner. Usage:

    .venv/bin/python scripts/sdk_smoke.py [scratch_dir]

Spawns one Claude session in a temp scratch dir and asks it to write a tiny
file. Streams every message both human-readable to stdout and JSON-line to
events.jsonl in the scratch dir. Prints final ResultMessage stats.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import tempfile
from pathlib import Path
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


PROMPT = """\
You are testing the Workforce mission runner harness.

In this directory, please:
1. Create a file called `haiku.txt` containing a 3-line haiku about test fixtures.
2. Then `cat haiku.txt` to confirm it.

Done. No commits, no further work.
"""


def to_jsonable(msg: Any) -> dict[str, Any]:
    """Convert an SDK message to a plain dict for JSONL logging.

    The SDK's messages and content blocks are dataclasses, so dataclasses.asdict
    handles them. We tag with the class name so consumers can branch.
    """
    if dataclasses.is_dataclass(msg):
        d = dataclasses.asdict(msg)
    else:
        d = {"repr": repr(msg)}
    d["_type"] = type(msg).__name__
    return d


def render(msg: Any) -> str | None:
    """Compact human-readable summary of one message; None to skip rendering."""
    if isinstance(msg, AssistantMessage):
        lines = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                lines.append(f"  text: {block.text!r}")
            elif isinstance(block, ToolUseBlock):
                lines.append(f"  tool_use: {block.name}({block.input})")
            elif isinstance(block, ThinkingBlock):
                lines.append(f"  thinking: {block.thinking[:80]!r}")
            else:
                lines.append(f"  {type(block).__name__}")
        return f"[assistant ({msg.model})]\n" + "\n".join(lines)
    if isinstance(msg, UserMessage):
        if isinstance(msg.content, str):
            return f"[user] {msg.content!r}"
        # List of blocks (typically tool_result).
        parts = []
        for block in msg.content:
            if isinstance(block, ToolResultBlock):
                content = block.content
                preview = repr(content)[:120]
                parts.append(f"  tool_result(err={block.is_error}): {preview}")
            else:
                parts.append(f"  {type(block).__name__}")
        return "[user]\n" + "\n".join(parts)
    if isinstance(msg, SystemMessage):
        return f"[system:{msg.subtype}]"
    if isinstance(msg, ResultMessage):
        return (
            f"[result subtype={msg.subtype} turns={msg.num_turns} "
            f"duration_ms={msg.duration_ms} cost=${msg.total_cost_usd or 0:.4f} "
            f"is_error={msg.is_error}]"
        )
    return f"[{type(msg).__name__}]"


async def run_smoke(scratch: Path) -> ResultMessage | None:
    options = ClaudeAgentOptions(
        cwd=str(scratch),
        allowed_tools=["Read", "Write", "Edit", "Bash", "Glob"],
        permission_mode="bypassPermissions",
        max_turns=10,
        max_budget_usd=0.50,
        system_prompt="You are a Workforce specialist running a smoke test. Be concise.",
    )
    log_path = scratch / "events.jsonl"
    final: ResultMessage | None = None
    print(f"scratch dir: {scratch}")
    print(f"event log:   {log_path}")
    print("---")
    with log_path.open("w") as log:
        async for msg in query(prompt=PROMPT, options=options):
            log.write(json.dumps(to_jsonable(msg), default=str) + "\n")
            log.flush()
            line = render(msg)
            if line:
                print(line)
            if isinstance(msg, ResultMessage):
                final = msg
    return final


def main() -> int:
    if len(sys.argv) > 1:
        scratch = Path(sys.argv[1])
        scratch.mkdir(parents=True, exist_ok=True)
    else:
        scratch = Path(tempfile.mkdtemp(prefix="workforce-smoke-"))

    try:
        final = asyncio.run(run_smoke(scratch))
    except KeyboardInterrupt:
        print("\n[smoke] interrupted")
        return 130
    except Exception as e:
        print(f"\n[smoke] crashed: {type(e).__name__}: {e}")
        raise

    print("---")
    if final is None:
        print("[smoke] no ResultMessage received — investigate")
        return 2
    if final.is_error:
        print(f"[smoke] mission ended with error: {final.errors}")
        return 1

    haiku = scratch / "haiku.txt"
    if haiku.is_file():
        print(f"[smoke] haiku.txt was written:\n{haiku.read_text()}")
    else:
        print("[smoke] haiku.txt was NOT written — model may have skipped the task")

    print(f"[smoke] OK. Files in scratch:")
    for p in sorted(scratch.iterdir()):
        print(f"  {p.name} ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
