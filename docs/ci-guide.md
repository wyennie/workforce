# CI Guide — What Runs, What It Requires, What Breaks It

## Overview

CI (`ci.yml`) runs on every push to `main` and on every pull request. A separate
`release.yml` pipeline runs when a `v*.*.*` tag is pushed and adds a build + publish
stage. The third workflow file (`workforce-example.yml`) is a copy-paste template for
users of the Workforce action — it is **not** a CI check for this repo.

---

## Workflow inventory

### `.github/workflows/ci.yml`

**Trigger:** push to `main`, any PR.

**Matrix:** Python 3.11, 3.12, 3.13 — `fail-fast: false` (all three versions run to
completion even if one fails).

| Step | Command | What it validates |
|------|---------|-------------------|
| Set up Python | `actions/setup-python@v5` with pip cache | Python version + pip caching |
| Install deps | `pip install -e '.[dev]'` | Installs workforce + all dev extras (pytest, mypy, ruff, fastapi, httpx, jinja2) |
| Lint | `ruff check` | Code style, import ordering, bugbear patterns, pyupgrade |
| Type check | `mypy` | Full strict type coverage for `src/workforce/` and `tests/` |
| Tests | `pytest -q` | Functional correctness; `-q` quiet mode, `-ra` shows all non-passed results |

### `.github/workflows/release.yml`

**Trigger:** push of a `v*.*.*` tag.

**Jobs (sequential):**

1. **test** — identical to `ci.yml` but on Python 3.11 only (single matrix entry).
2. **build** (needs: test) — `pip install build` → `python -m build` → uploads `dist/` as
   an artifact. Fails if no files are found.
3. **publish** (needs: build) — downloads `dist/`, runs `twine upload dist/*` to PyPI,
   then creates a GitHub Release with auto-generated notes and attaches `dist/*`.

**Shell injection note:** `${{ github.ref_name }}` is interpolated directly into the
`run:` script for `gh release create`. Git tag names are controlled by whoever pushed
the tag (typically a maintainer), not arbitrary user input, so the practical risk is
low — but a tag name containing shell metacharacters would cause unexpected behavior.
Using an env-var intermediate (as `action.yml` does for all its inputs) would be
strictly safer.

### `action.yml` (composite action for users)

Not a CI check for this repo. Noteworthy: all `${{ inputs.* }}` values are passed via
`env:` variables (e.g., `INPUT_TICKET`, `INPUT_SPECIALIST`) before being referenced in
the shell script — this is the correct pattern and avoids shell injection.

---

## Ruff (`ruff check`)

**Configuration** (`pyproject.toml`, `[tool.ruff.lint]`):

| Rule set | Category |
|----------|----------|
| `E` | pycodestyle errors |
| `F` | pyflakes (undefined names, unused imports) |
| `W` | pycodestyle warnings |
| `I` | isort (import ordering) |
| `B` | flake8-bugbear (likely bugs) |
| `UP` | pyupgrade (modernize syntax) |

**Ignored rules:**
- `E501` — line length (capped via formatter; long URLs/strings are allowed).
- `B008` — function calls in default arguments (Typer's entire API relies on this).

**Per-file ignore:**
- `tests/*` — `E402` is exempted. Test files sometimes need imports after fixture setup
  code; use `# noqa: E402` comments or rely on the blanket exemption.

### Common ruff failure patterns

**I001 / import ordering (isort):** ruff enforces stdlib → third-party → local import
groups, separated by blank lines. `from __future__ import annotations` must be the
absolute first import if present. Mixing groups or placing local imports before
third-party ones fails `I`. Every `.py` in `src/workforce/` uses the future-annotations
import and maintains this ordering; new files must do the same.

**F401 / unused imports:** Any symbol imported but never referenced fails. This
commonly bites when refactoring removes a use site but leaves the import line. Check
`from typing import Any, Literal` — if `Literal` is removed from function signatures,
it must be removed from the import too.

**B009 / getattr with constant attribute:** `getattr(obj, "attr")` should be
`obj.attr`. A recent commit (`2842f2a`) fixed a B009 violation introduced by a prior
mypy-fix pass — the pattern can appear when translating `hasattr` guards into explicit
attribute access.

**UP / old-style type hints:** The codebase uses `X | Y` unions, `list[str]` generics,
and `dict[str, Any]` throughout (no `Optional`, `Union`, `List`, `Dict` from
`typing`). Writing `Optional[str]` instead of `str | None` will fail `UP007`.
`from __future__ import annotations` is required for `X | Y` syntax to work at
runtime on Python 3.11+ class bodies and default values.

---

## mypy (`mypy --strict`)

**Configuration:**
```toml
[tool.mypy]
python_version = "3.11"
strict = true
files = ["src/workforce", "tests"]
```

Both `src/workforce/` **and** `tests/` are type-checked. Strict mode enables, among
other things:
- `disallow_untyped_defs` — every function must have type annotations.
- `disallow_any_generics` — bare `list` or `dict` without type args fails.
- `warn_return_any` — a function returning `Any` when the caller expects a concrete
  type is flagged.
- `warn_unused_ignores` — a `# type: ignore` comment that no longer suppresses
  anything becomes an error itself.

### Common mypy failure patterns

**Missing return-type annotation:** Any new function — including test helpers and
inner/nested functions — must declare `-> ReturnType`. Test helper lambdas and
closures inside test functions are not type-checked at this depth, but any `def` at
module level or inside a class is. Forgetting `-> None` on a procedure is the most
frequent omission.

**Untyped test helpers:** Because `tests/` is under mypy coverage, all test utility
functions (`make_spec()`, `_result()`, `_assistant()`, etc.) must carry full
annotations. If you add a new shared helper, annotate it.

**Optional-dependency imports:** `fastapi`, `uvicorn`, `jinja2`, and `mcp` are not
always installed. Modules that import them must guard with
`# type: ignore[import-not-found]`. FastAPI's route decorators additionally need
`# type: ignore[untyped-decorator]` because FastAPI's stubs mark decorators as
returning `Any`. Missing either comment fails mypy; a comment that's no longer
needed (because the dependency was added to dev extras or stubs improved) fails
`warn_unused_ignores`.

**`Any` propagation:** Several internal functions (`run_specialist`, `message_to_jsonable`,
renderers) deliberately accept or return `Any` to bridge the untyped SDK boundary.
When adding new call sites that receive those return values, avoid immediately
assigning to a typed variable — do the cast explicitly or re-annotate. If mypy infers
the type as `Any` and you then pass it to a typed function, it may silently succeed at
the assignment site but fail later.

**`warn_unused_ignores` churn:** The mypy-fix commits (`f79d418` through `2842f2a`)
show that fixing one mypy error can expose a previously-suppressed `# type: ignore`
elsewhere, turning it into an "unused ignore" error. When resolving mypy errors, scan
for stale `# type: ignore` comments in the same area.

**Async generator annotations:** `runner._single_message_stream` and
`reviewer._prompt_to_stream` are both declared `-> AsyncIterator[dict[str, Any]]`
despite being async generators (which technically return `AsyncGenerator`). mypy
accepts this because `AsyncGenerator` is a subtype of `AsyncIterator`. If either
function is refactored to a non-generator async function that returns an
`AsyncIterator` from somewhere else, the annotation would need updating.

---

## pytest

**Configuration:**
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-ra"
```

`pytest-asyncio` is installed but **no `asyncio_mode` is configured**. All async
operations in tests use `asyncio.run(...)` explicitly. Do **not** write:

```python
# BAD — silently does nothing; pytest sees a coroutine, counts it as passed
async def test_something() -> None:
    result = await some_async_function()
    assert result == expected
```

Write instead:

```python
# CORRECT
def test_something() -> None:
    result = asyncio.run(some_async_function())
    assert result == expected
```

A coroutine-returning test function would not be flagged by pytest (no asyncio mode
configured) and would silently "pass" without running any assertions.

### Mock boundary

All tests mock at the SDK boundary: `claude_agent_sdk.query` is patched via
`unittest.mock.patch.object(runner, "query", fake)`. Tests never make real API calls.
The `fake_query_factory` helper in `test_runner.py` is the canonical pattern; copy
it rather than inventing ad-hoc mocks.

### Optional-dependency tests

`tests/test_web.py` uses `pytest.importorskip("fastapi")` to skip itself when FastAPI
is not installed. Because `dev` extras include FastAPI, the skip never fires in CI —
but it's important to keep the guard so local development without the optional extras
still works.

### Test file structure

There is no `conftest.py`. Each test file is self-contained: it imports the module
under test, patches at the SDK boundary if needed, and creates any temp-path fixtures
via pytest's `tmp_path`. When adding a new module `src/workforce/foo.py`, the expected
test file is `tests/test_foo.py`.

---

## Cross-cutting patterns to watch

### 1. `from __future__ import annotations` — everywhere

Every `.py` in `src/workforce/` (except the two trivial `__init__.py` files and
`version.py`) carries `from __future__ import annotations` as its first import. All
32 test files do the same. New files must include it. Its absence won't always produce
an immediate error, but it will cause runtime `TypeError` when code tries to evaluate
annotations (e.g., via `get_type_hints`) and will prevent `X | Y` union syntax in
Pydantic model fields on Python 3.11.

### 2. The `_single_message_stream` / `_prompt_to_stream` SDK coupling

`runner._single_message_stream` and `reviewer._prompt_to_stream` both hand-craft the
internal streaming-input dict format that the SDK expects when `can_use_tool` is set.
The format is:
```python
{"type": "user", "session_id": "", "message": {"role": "user", "content": text}, "parent_tool_use_id": None}
```
This copies an internal SDK format (not a public API). `test_runner.py::test_single_message_stream_shape`
is the regression guard — if the SDK changes the format, this test fails explicitly.
When upgrading `claude-agent-sdk`, run the full test suite and pay extra attention to
this test.

### 3. Duplicate tool-summarizer functions

`cli/_common.py` and `cli/dispatch.py` each have their own version of the renderer /
tool-summarizer helper (`_make_renderer`, `_make_sub_renderer`, `_make_manager_renderer`).
They have diverged over time. Neither ruff nor mypy will catch this drift — it's a
human review concern. When modifying one, check whether the other needs the same
change.

### 4. Matrix vs. mypy target version

mypy targets Python 3.11 (`python_version = "3.11"`) even though CI also tests 3.12
and 3.13. Type stubs and mypy behavior can differ between mypy versions but the mypy
version pinned in dev extras (`mypy>=1.10`) is a lower bound, not an exact pin. If
mypy introduces new strict checks in a patch release, CI can start failing without any
code change.

### 5. `ProjectConfig` silently drops unknown keys

`ProjectConfig` uses `extra='ignore'`, so any key written to `.workforce.toml` that
isn't in the model is silently discarded on load. This means adding a new config field
requires: (a) adding it to `ProjectConfig`, (b) updating any write paths, and (c)
being aware that existing `.workforce.toml` files in the wild won't have it.

### 6. `fcntl` conditional import

`specialist.py` and `mission.py` both have:
```python
try:
    import fcntl as _fcntl
except ImportError:
    _fcntl = None  # Windows
```
mypy handles this correctly via `types.ModuleType | None`. Any new file that needs
file locking must follow this exact pattern; a bare `import fcntl` would fail on
Windows (and may fail in some CI environments).

---

## Quick checklist before pushing

- [ ] All new `.py` files start with `from __future__ import annotations`.
- [ ] All new functions (including test helpers) have complete type annotations.
- [ ] New imports are in the right group (stdlib / third-party / local), separated by blank lines.
- [ ] Optional-dependency imports (`fastapi`, `mcp`, etc.) carry `# type: ignore[import-not-found]`.
- [ ] Async test logic is wrapped in `asyncio.run(...)`, not in `async def test_`.
- [ ] Any `# type: ignore` added is the narrowest possible (use `[error-code]` form).
- [ ] After fixing a mypy error, scan nearby `# type: ignore` comments for stale ones.
- [ ] When changing `runner._single_message_stream` or `reviewer._prompt_to_stream`,
  verify `test_runner.py::test_single_message_stream_shape` still passes.
