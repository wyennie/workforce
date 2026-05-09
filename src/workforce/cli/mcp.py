"""CLI command for launching the workforce MCP server."""

from __future__ import annotations


def mcp_server_command() -> None:
    """Start the workforce MCP server on stdio.

    Requires the ``mcp`` optional dependency::

        pip install "workforce-ai[mcp]"

    Configure in Claude Code's ``~/.claude/claude.json``::

        {
          "mcpServers": {
            "workforce": {
              "command": "workforce",
              "args": ["mcp-server"]
            }
          }
        }
    """
    from workforce.mcp.server import main

    main()
