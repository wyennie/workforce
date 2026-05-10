"""Workforce MCP server.

Exposes four tools over the MCP stdio transport so that any MCP-capable Claude
agent can dispatch missions, query status, and read results.

Requires the optional ``mcp`` extra::

    pip install "workforce-ai[mcp]"
"""

from __future__ import annotations

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
except ImportError:
    print("MCP package not installed. Run: pip install workforce-ai[mcp]")
    raise SystemExit(1) from None

import json
from typing import Any

from workforce.mcp.tools import (
    workforce_dispatch,
    workforce_mission_result,
    workforce_mission_status,
    workforce_roster,
)

server = Server("workforce")


@server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
async def list_tools() -> list[Tool]:
    """Advertise the four workforce tools to the MCP client."""
    return [
        Tool(
            name="workforce_dispatch",
            description=(
                "Dispatch a new mission to a workforce specialist. "
                "Returns immediately with the mission ID and final status."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name or ID.",
                    },
                    "ticket": {
                        "type": "string",
                        "description": "Task description or ticket text.",
                    },
                    "specialist": {
                        "type": "string",
                        "description": "Specialist name (optional; auto-selected if omitted).",
                    },
                    "auto_merge": {
                        "type": "boolean",
                        "description": "Merge the branch automatically when the mission completes.",
                    },
                },
                "required": ["project", "ticket"],
            },
        ),
        Tool(
            name="workforce_mission_status",
            description="Fetch the current status and metadata for a mission by ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mission_id": {
                        "type": "string",
                        "description": "Mission ID returned by workforce_dispatch.",
                    },
                },
                "required": ["mission_id"],
            },
        ),
        Tool(
            name="workforce_roster",
            description="List all specialists in the roster with cumulative mission stats.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="workforce_mission_result",
            description="Read the result.md narrative written by the specialist at mission end.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mission_id": {
                        "type": "string",
                        "description": "Mission ID.",
                    },
                },
                "required": ["mission_id"],
            },
        ),
    ]


@server.call_tool()  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch the named tool and return the result as TextContent."""
    if name == "workforce_dispatch":
        dispatch_result = workforce_dispatch(**arguments)
        return [TextContent(type="text", text=json.dumps(dispatch_result))]
    if name == "workforce_mission_status":
        status_result = workforce_mission_status(**arguments)
        return [TextContent(type="text", text=json.dumps(status_result))]
    if name == "workforce_roster":
        roster_result = workforce_roster()
        return [TextContent(type="text", text=json.dumps(roster_result))]
    if name == "workforce_mission_result":
        mission_result = workforce_mission_result(**arguments)
        return [TextContent(type="text", text=mission_result)]
    raise ValueError(f"Unknown tool: {name}")


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Entry point: start the MCP server on stdio."""
    import asyncio

    asyncio.run(_run())
