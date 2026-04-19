"""Shared internal utilities used across workforce modules."""

from __future__ import annotations

import re
from typing import Any

import tomli_w

# Matches fenced code blocks (optionally tagged ``json``) used in LLM output.
# Shared by mission.py, manager.py, and reviewer.py.
_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


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
