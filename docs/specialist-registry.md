# Specialist Registry Format

The Workforce specialist marketplace is hosted at
[workforce-ai/specialists](https://github.com/workforce-ai/specialists).  
Anyone can contribute a specialist by opening a pull request.

---

## Repository Layout

```
specialists/
    index.json                  # machine-readable catalogue
    <slug>/
        specialist.toml         # the specialist definition
        README.md               # human-readable description
```

`<slug>` is a lowercase identifier matching the pattern `[a-z][a-z0-9_-]{0,31}`.

---

## specialist.toml

The format is identical to the TOML that Workforce writes to
`~/.workforce/roster/<name>/specialist.toml`, minus the runtime-only
fields that live in separate files (`memory.md`, `stats.json`).

**Required fields:**

| Field            | Type            | Description                                          |
|------------------|-----------------|------------------------------------------------------|
| `schema_version` | integer (= `1`) | Schema version; must be `1`.                         |
| `name`           | string          | Slug used as the default local install name.         |
| `role`           | string          | One-line role description shown in `workforce roster`.|
| `model`          | string          | Claude model id (e.g. `claude-sonnet-4-6`).          |
| `allowed_tools`  | list of strings | Tools the specialist may call.                       |
| `base_prompt`    | string          | Full base system prompt, including the `## Role` section. |

**Example:**

```toml
schema_version = 1
name = "backend-go"
role = "Go backend engineer. gRPC services, database migrations, and the boring parts."
model = "claude-sonnet-4-6"
allowed_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebFetch"]

base_prompt = """
You are operating inside a Workforce mission. The mission runner has placed
you in a git worktree on a fresh branch. Do the work, commit it, and finish.

## Role

You are a senior Go backend engineer. You prefer the standard library over
third-party frameworks. You write idiomatic Go: errors are values, interfaces
are small, goroutines are explicit.

When you add a dependency, verify there is no stdlib alternative. When you
write a migration, consider rollback. When you expose an RPC, think about
versioning.
"""
```

---

## index.json

A flat JSON array listing every available specialist.  
Kept in sync with the individual directories via CI.

**Entry shape:**

```jsonc
{
  "slug":           "backend-go",          // directory name
  "description":    "Go backend engineer …",  // one-line summary
  "templates_used": ["backend"],           // base templates (informational)
  "author":         "alice"               // GitHub username
}
```

**Full example:**

```json
[
  {
    "slug": "backend-go",
    "description": "Go backend engineer. gRPC services, database migrations, and the boring parts.",
    "templates_used": ["backend"],
    "author": "alice"
  },
  {
    "slug": "frontend-react",
    "description": "React + TypeScript specialist. Components, hooks, and accessibility.",
    "templates_used": ["frontend"],
    "author": "bob"
  }
]
```

---

## CLI Quick Reference

```bash
# Browse the marketplace
workforce specialist search
workforce specialist search go

# Install a specialist
workforce specialist install backend-go
workforce specialist install backend-go --name my-go

# Export your own specialist for publishing
workforce specialist publish myspec --output-dir ./publish/
```

---

## Contributing a Specialist

1. Fork [workforce-ai/specialists](https://github.com/workforce-ai/specialists).
2. Create `specialists/<slug>/specialist.toml` and `specialists/<slug>/README.md`.  
   Use `workforce specialist publish <name>` to generate a template from your
   existing local specialist.
3. Add an entry to `specialists/index.json`.
4. Open a pull request. CI validates the TOML is parseable and the index entry
   is consistent with the directory.

**Guidelines:**

- The `name` field in `specialist.toml` must equal the directory `<slug>`.
- Omit any private instructions, proprietary context, or personal API keys.
- The `base_prompt` should be self-contained — assume the reader has no
  context about your personal projects.
- Keep the role focused: one specialist, one domain.
