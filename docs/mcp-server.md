# Workforce MCP Server

## What is MCP?

The [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) is an open standard
that lets AI assistants (Claude, etc.) discover and call external tools at runtime. An MCP
server exposes a set of **tools** over a well-defined JSON-RPC protocol. The client (e.g.
Claude Code) connects to the server, lists available tools, and calls them during a
conversation — exactly like built-in tool use, but for arbitrary external services.

Workforce ships an MCP server so that any MCP-capable Claude agent or IDE integration can
dispatch missions, query status, and read results without leaving the conversation.


## The Four Tools

### `workforce_dispatch`

Dispatch a new mission to a specialist and return immediately with the mission ID.

**Input schema:**
```json
{
  "type": "object",
  "properties": {
    "project":     { "type": "string",  "description": "Project name or ID" },
    "ticket":      { "type": "string",  "description": "Task description or ticket text" },
    "specialist":  { "type": "string",  "description": "Specialist name (optional; auto-selected if omitted)" },
    "auto_merge":  { "type": "boolean", "description": "Merge the branch automatically when the mission completes" }
  },
  "required": ["project", "ticket"]
}
```

**Returns:** `{ "mission_id": "...", "status": "completed", "branch": "workforce/..." }`
or `{ "error": "..." }` on failure.

---

### `workforce_mission_status`

Fetch the current status of a mission by ID. Reads the mission's `meta.json` artifact
directly — no running process required.

**Input schema:**
```json
{
  "type": "object",
  "properties": {
    "mission_id": { "type": "string", "description": "Mission ID returned by workforce_dispatch" }
  },
  "required": ["mission_id"]
}
```

**Returns:** The full mission meta dict (status, cost, branch, commits, etc.) or
`{ "error": "mission <id> not found" }`.

---

### `workforce_roster`

List every specialist in the roster together with cumulative stats.

**Input schema:**
```json
{
  "type": "object",
  "properties": {}
}
```

**Returns:** Array of objects:
```json
[
  {
    "name": "builder",
    "role": "Senior backend engineer …",
    "missions": 42,
    "cost_usd": 3.14
  }
]
```

---

### `workforce_mission_result`

Read the `result.md` narrative written by the specialist at the end of a mission.

**Input schema:**
```json
{
  "type": "object",
  "properties": {
    "mission_id": { "type": "string", "description": "Mission ID" }
  },
  "required": ["mission_id"]
}
```

**Returns:** The raw Markdown text of `result.md`, or an error string if not found.


## Deployment: stdio transport

The server communicates over **stdin/stdout** using newline-delimited JSON-RPC 2.0
messages (the MCP "stdio" transport). This means:

- No network port to configure or firewall.
- The MCP client spawns the server as a subprocess and owns its lifecycle.
- ANTHROPIC_API_KEY is passed via the environment (inherited from the client).

Start the server directly:

```bash
ANTHROPIC_API_KEY=sk-ant-… workforce mcp-server
```

The process blocks, reading from stdin and writing to stdout, until the client closes the
connection.


## Authentication

The server does not implement its own authentication layer. It relies on:

1. **`ANTHROPIC_API_KEY`** — inherited from the environment. Any `workforce dispatch` call
   made by the server will use this key.
2. **Process-level access control** — the MCP client spawns the server; whoever can run
   the client process controls access.

Never expose the stdio server over a network socket without adding your own auth wrapper.


## Claude Code configuration

Add the server to `~/.claude/claude.json` (or the project-level `.claude/claude.json`):

```json
{
  "mcpServers": {
    "workforce": {
      "command": "workforce",
      "args": ["mcp-server"],
      "env": {
        "ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"
      }
    }
  }
}
```

After restarting Claude Code the four workforce tools appear in the tool picker. You can
then ask Claude to dispatch missions, check their status, and read results without leaving
the chat.


## Installing the optional dependency

The MCP server requires the `mcp` package, which is not installed by default:

```bash
pip install "workforce-ai[mcp]"
# or via uv:
uv pip install "workforce-ai[mcp]"
```
