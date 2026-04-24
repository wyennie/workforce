# Events Schema

This document describes the JSONL event format written to `events.jsonl` during
a Workforce mission, and the other per-mission artifact files.

---

## events.jsonl

Every message streamed from `claude_agent_sdk.query()` is written to
`events.jsonl` as a single JSON object per line (newline-delimited JSON /
JSONL). Each record contains a `_type` field with the Python class name of the
SDK message. Records are flushed immediately on receipt, so the file is
safe to tail while a mission is running.

The file is written by `runner.run_specialist()` via `message_to_jsonable()` in
`src/workforce/runner.py`. SDK dataclass instances are serialized with
`dataclasses.asdict()`; unknown types fall back to `{"repr": "..."}`.

### Message types

#### `SystemMessage`

Emitted once at the start of every session. Carries the session initialisation
payload: working directory, session identifier, and the full list of tools
available to the specialist.

**Key fields:**

| Field | Type | Description |
|---|---|---|
| `_type` | `"SystemMessage"` | Message type discriminator |
| `subtype` | `str` | Always `"init"` for session start |
| `data.cwd` | `str` | Absolute working directory for this session |
| `data.session_id` | `str` | UUID that identifies the session; used by `--resume` |
| `data.tools` | `list[str]` | Tool names the specialist is allowed to invoke |

**Example:**

```json
{
  "_type": "SystemMessage",
  "subtype": "init",
  "data": {
    "type": "system",
    "subtype": "init",
    "cwd": "/home/will/.workforce/projects/a1b2c3d4e5f6/worktrees/m-20260512-090000-ab12",
    "session_id": "7681280c-2b76-4b7b-a246-fc7bad48667b",
    "tools": ["Bash", "Edit", "Glob", "Grep", "Read", "Write"]
  }
}
```

---

#### `AssistantMessage`

One Claude response turn. The `content` array contains one or more blocks:

- **TextBlock** — prose text or code; rendered in `transcript.md`.
  `{"text": "..."}`
- **ToolUseBlock** — a tool call; has `id`, `name`, and `input`.
  `{"id": "toolu_...", "name": "Bash", "input": {"command": "..."}}`
- **ThinkingBlock** — extended thinking output (when the model reasons before
  replying); has `thinking` and an opaque `signature`.
  `{"thinking": "...", "signature": "..."}`

**Key fields:**

| Field | Type | Description |
|---|---|---|
| `_type` | `"AssistantMessage"` | Message type discriminator |
| `content` | `list` | One or more content blocks (see above) |
| `model` | `str` | Model used for this turn, e.g. `"claude-sonnet-4-6"` |
| `session_id` | `str` | Matches the `SystemMessage.data.session_id` |
| `usage.input_tokens` | `int` | Prompt tokens billed for this turn |
| `usage.output_tokens` | `int` | Completion tokens billed for this turn |
| `usage.cache_read_input_tokens` | `int` | Tokens served from the prompt cache |
| `usage.cache_creation_input_tokens` | `int` | Tokens written to the prompt cache |
| `stop_reason` | `str \| null` | `"end_turn"`, `"tool_use"`, or `null` if mid-stream |
| `message_id` | `str` | SDK-internal message identifier |

**Example (TextBlock):**

```json
{
  "_type": "AssistantMessage",
  "content": [
    {"text": "Now let me read the existing tests before making changes:"}
  ],
  "model": "claude-sonnet-4-6",
  "session_id": "7681280c-2b76-4b7b-a246-fc7bad48667b",
  "parent_tool_use_id": null,
  "error": null,
  "stop_reason": null,
  "usage": {
    "input_tokens": 1,
    "output_tokens": 12,
    "cache_read_input_tokens": 19582,
    "cache_creation_input_tokens": 1794
  }
}
```

**Example (ToolUseBlock):**

```json
{
  "_type": "AssistantMessage",
  "content": [
    {
      "id": "toolu_01QLmLcbeiWyaaviX2USayyS",
      "name": "Bash",
      "input": {
        "command": "git log --oneline -5 && ls -la",
        "description": "Check git state and directory contents"
      }
    }
  ],
  "model": "claude-sonnet-4-6",
  "session_id": "7681280c-2b76-4b7b-a246-fc7bad48667b",
  "parent_tool_use_id": null,
  "error": null,
  "stop_reason": "tool_use",
  "usage": {
    "input_tokens": 2,
    "output_tokens": 58,
    "cache_read_input_tokens": 9618,
    "cache_creation_input_tokens": 3822
  }
}
```

---

#### `UserMessage`

Tool results fed back to Claude. Every `AssistantMessage` with a `ToolUseBlock`
is followed by a `UserMessage` containing the result.

**Key fields:**

| Field | Type | Description |
|---|---|---|
| `_type` | `"UserMessage"` | Message type discriminator |
| `content` | `list` | One or more result blocks (see below) |
| `uuid` | `str` | Internal record UUID |
| `parent_tool_use_id` | `str \| null` | The `id` from the corresponding `ToolUseBlock`, or `null` for injected user turns |

`content` block shapes:

- **ToolResultBlock** — result of a tool call.
  `{"tool_use_id": "toolu_...", "content": "<stdout>", "is_error": false}`
  When `is_error` is `true`, `content` contains the error text.
- **TextBlock** — plain text injected by the orchestrator as a user turn (e.g.
  Reviewer feedback). `{"text": "..."}`

**Example (ToolResultBlock):**

```json
{
  "_type": "UserMessage",
  "content": [
    {
      "tool_use_id": "toolu_01QLmLcbeiWyaaviX2USayyS",
      "content": "8694fc0 initial\n---\ntotal 12\n...",
      "is_error": false
    }
  ],
  "uuid": "813400fd-bac9-42f9-b520-2ea98f47c8b2",
  "parent_tool_use_id": null
}
```

---

#### `ResultMessage`

The final message in every session. Carries aggregate stats for the run.
There is exactly one `ResultMessage` per session (if the session ends cleanly);
there may be zero if the session was interrupted or the SDK crashed.

**Key fields:**

| Field | Type | Description |
|---|---|---|
| `_type` | `"ResultMessage"` | Message type discriminator |
| `subtype` | `str` | `"success"` on clean exit; `"error_max_turns"` etc. on limit |
| `is_error` | `bool` | `true` if the session ended in an error state |
| `num_turns` | `int` | Number of assistant turns completed |
| `session_id` | `str` | Matches the `SystemMessage.data.session_id` |
| `stop_reason` | `str` | `"end_turn"`, `"max_turns"`, `"budget_exceeded"` etc. |
| `total_cost_usd` | `float` | Total API cost in USD for this session |
| `duration_ms` | `int` | Wall-clock duration from session start to `ResultMessage` |
| `usage.input_tokens` | `int` | Cumulative prompt tokens across all turns |
| `usage.output_tokens` | `int` | Cumulative completion tokens across all turns |
| `errors` | `list[str] \| null` | Error messages when `is_error=true` |

**Example:**

```json
{
  "_type": "ResultMessage",
  "subtype": "success",
  "is_error": false,
  "num_turns": 39,
  "session_id": "7681280c-2b76-4b7b-a246-fc7bad48667b",
  "stop_reason": "end_turn",
  "total_cost_usd": 1.2136,
  "duration_ms": 495768,
  "duration_api_ms": 491816,
  "usage": {
    "input_tokens": 38,
    "output_tokens": 32917,
    "cache_read_input_tokens": 1582235,
    "cache_creation_input_tokens": 64827,
    "service_tier": "standard"
  }
}
```

---

### Other event types

The SDK may emit additional message types that are recorded verbatim in
`events.jsonl`:

| `_type` | When emitted |
|---|---|
| `RateLimitEvent` | API rate-limit info sent alongside responses |
| `TaskStartedMessage` | A sub-agent task was dispatched (`subtype: "task_started"`) |
| `TaskProgressMessage` | Progress update from a running sub-agent task |
| `TaskNotificationMessage` | Sub-agent task completed or failed |

These types pass through `message_to_jsonable()` unchanged and are available in
the log for replay and debugging, but Workforce's CLI does not render them in
the live output or transcript.

---

## Mission artifact files

Each mission creates a directory at:

```
~/.workforce/projects/<project-id>/missions/<mission-id>/
```

The following files are written there:

### `ticket.md`

**Written at:** Mission start (before the specialist runs).

The verbatim ticket text, as passed to `dispatch_command`. Preserved so the
mission is self-contained — you can audit what was asked without needing the
original CLI invocation. Trailing whitespace is stripped; a single trailing
newline is added.

---

### `events.jsonl`

**Written at:** Continuously during the specialist run (one record per SDK
message, flushed immediately).

JSONL stream of every message received from `claude_agent_sdk.query()`. See
[Message types](#message-types) above. The file is created at the start of the
run and closed when the SDK session ends (or the wall timeout is hit). For
missions with a Reviewer loop, additional rounds are appended to the same file.

A companion `stderr.log` is written alongside it to capture the `claude` CLI's
standard error output, which contains diagnostics when the SDK reports a generic
failure.

---

### `transcript.md`

**Written at:** Mission end (after the runner returns).

Human-readable view of all `AssistantMessage` turns: only `TextBlock` content
is included (no tool use, no thinking blocks). Multiple turns are separated by
`---` rules. Empty if the session produced no text output (e.g. timed out on
the first turn).

---

### `result.md`

**Written at:** Mission end (after `transcript.md`).

The single best "summary" of what happened:

1. If a memory delta was captured, uses `MemoryDelta.summary`.
2. Otherwise, uses the last non-empty `TextBlock` from any `AssistantMessage`.
3. Falls back to `"(no summary captured)"` if neither is available.

This is what `workforce mission show` displays as the mission summary.

---

### `meta.json`

**Written at:** Mission end (atomically, via a temp file + `os.replace`).

JSON serialization of `MissionMeta` (for single missions) or
`ParallelMissionMeta` (for multi-task parallel dispatch). Key fields:

| Field | Type | Description |
|---|---|---|
| `schema_version` | `int` | Currently `1` |
| `mission_id` | `str` | `m-YYYYMMDD-HHMMSS-xxxx` |
| `project_id` | `str` | 12-hex project identifier |
| `project_name` | `str` | Human display name |
| `specialist` | `str` | Specialist name that ran the mission |
| `model` | `str` | Model used, e.g. `"claude-sonnet-4-6"` |
| `ticket` | `str` | Verbatim ticket text |
| `branch` | `str \| null` | `workforce/<mission-id>` branch; `null` for workspace projects |
| `worktree_path` | `str \| null` | Absolute path of the git worktree; the workspace dir for workspace projects |
| `base_sha` | `str \| null` | Git SHA the worktree was forked from; `null` for workspace |
| `started_at` | `str` | ISO-8601 UTC timestamp, e.g. `"2026-05-12T09:00:00Z"` |
| `ended_at` | `str` | ISO-8601 UTC timestamp |
| `duration_seconds` | `float` | Wall-clock time from start to end |
| `status` | `str` | One of `completed`, `error`, `wall_timeout`, `interrupted`, `review_rejected` |
| `error_detail` | `str \| null` | Human-readable failure description; `null` on success |
| `cost_usd` | `float` | Total API cost (specialist + memory delta + manager + reviewer) |
| `manager_cost_usd` | `float` | Manager planning cost (0 when `--specialist` bypass used) |
| `review_cost_usd` | `float` | Sum of all Reviewer rounds' costs |
| `turn_count` | `int` | Number of assistant turns from the `ResultMessage` |
| `commits` | `list` | Git commits added by this mission (`sha`, `subject`, `body`) |
| `memory_delta_captured` | `bool` | Whether the post-mission memory extraction succeeded |
| `reviews` | `list` | Reviewer round records (`round`, `approved`, `summary`, `issues`, `cost_usd`) |
| `revision_rounds` | `int` | How many times the specialist re-ran in response to the Reviewer |

`meta.json` is written atomically to prevent torn reads by concurrent
`workforce project tail` or `workforce missions` watchers. Parallel parent
missions have an additional `parent_mission_id` field and contain `sub_missions`
instead of `commits` and `reviews`.

**Example (completed single mission):**

```json
{
  "schema_version": 1,
  "mission_id": "m-20260512-090000-ab12",
  "project_id": "a1b2c3d4e5f6",
  "project_name": "my-service",
  "specialist": "builder",
  "model": "claude-sonnet-4-6",
  "ticket": "Add rate limiting to the /api/search endpoint.",
  "branch": "workforce/m-20260512-090000-ab12",
  "worktree_path": "/home/alice/.workforce/projects/a1b2c3d4e5f6/worktrees/m-20260512-090000-ab12",
  "base_sha": "3f9a1b2c4d5e6f7a8b9c0d1e2f3a4b5c",
  "started_at": "2026-05-12T09:00:00Z",
  "ended_at": "2026-05-12T09:08:17Z",
  "duration_seconds": 497.0,
  "status": "completed",
  "error_detail": null,
  "cost_usd": 0.8341,
  "manager_cost_usd": 0.0,
  "review_cost_usd": 0.0,
  "turn_count": 31,
  "commits": [
    {
      "sha": "c0ffee1234567890abcdef1234567890abcdef12",
      "subject": "feat(api): add rate limiting to /api/search",
      "body": "Uses Redis sliding window via redis-py. Limit is 60 req/min per IP.\nConfigurable via RATE_LIMIT_SEARCH env var.\n\nCo-Authored-By: builder <builder@workforce.local>"
    }
  ],
  "memory_delta_captured": true,
  "reviews": [],
  "revision_rounds": 0
}
```
