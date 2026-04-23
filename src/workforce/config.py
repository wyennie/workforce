"""Global Workforce configuration loaded from ~/.workforce/config.toml.

Keys defined here act as lowest-priority defaults; explicit CLI flags always
take precedence. An absent or malformed config.toml is silently ignored so
new installs work out of the box without any setup.
"""

from __future__ import annotations

import sys
import warnings
from typing import Optional

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

from pydantic import BaseModel


class GlobalConfig(BaseModel):
    """User-editable global defaults for Workforce.

    All fields are optional; omitted fields leave the CLI's own defaults in
    effect.
    """

    default_model: Optional[str] = None
    max_turns: Optional[int] = None
    max_cost: Optional[float] = None


def load_global_config() -> GlobalConfig:
    """Read and return the global config from ~/.workforce/config.toml.

    Returns a default (all-None) :class:`GlobalConfig` if the file is missing
    or cannot be parsed, so callers never need to handle errors.
    """
    from workforce import paths

    config_file = paths.config_path()
    if not config_file.exists():
        return GlobalConfig()
    try:
        data = tomllib.loads(config_file.read_text())
        return GlobalConfig(**data)
    except Exception as e:
        warnings.warn(f"Could not parse config.toml: {e}")
        return GlobalConfig()
