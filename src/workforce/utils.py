"""Shared internal utilities used across workforce modules."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import tomli_w

# Matches fenced code blocks (optionally tagged ``json``) used in LLM output.
# Shared by mission.py, manager.py, and reviewer.py.
_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _atomic_write(path: Path, content: str) -> None:
    """Atomically write *content* to *path* via a temp file and ``os.replace``.

    The temp file is placed next to *path* using the name ``<name>.tmp``
    (e.g. ``meta.json.tmp``) so the rename stays on the same filesystem.

    If ``os.replace`` raises :exc:`OSError`, the temp file is deleted before
    the exception is re-raised so no orphaned ``.tmp`` files are left behind.

    Args:
        path: Destination file path.
        content: Text content to write (UTF-8).
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _dump_toml(data: dict[str, Any]) -> str:
    """Serialize *data* to TOML text.

    Multi-line string values are written as TOML literal multi-line strings
    rather than escaped single-line strings, which keeps prompts and role
    descriptions readable when inspecting the output.

    Args:
        data: Mapping to serialize.

    Returns:
        TOML-formatted string.
    """
    return tomli_w.dumps(data, multiline_strings=True)
