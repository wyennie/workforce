"""Shared internal utilities used across workforce modules."""

from __future__ import annotations

import re
from typing import Any

import tomli_w

# Matches fenced code blocks (optionally tagged ``json``) used in LLM output.
# Shared by mission.py, manager.py, and reviewer.py.
_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _dump_toml(data: dict[str, Any]) -> str:
    """Serialize to TOML. Multi-line strings rendered literally for readability."""
    return tomli_w.dumps(data, multiline_strings=True)
