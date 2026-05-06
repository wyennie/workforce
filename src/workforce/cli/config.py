"""CLI commands for reading and writing the global Workforce config file.

``workforce config get`` prints the current effective settings.
``workforce config set KEY VALUE`` writes a key back to config.toml.
"""

from __future__ import annotations

import tomllib

import typer
from rich.table import Table

from workforce import output, paths
from workforce.config import GlobalConfig, load_global_config
from workforce.utils import _dump_toml

sub = typer.Typer(
    name="config",
    help="Read and write the global Workforce config (~/.workforce/config.toml).",
    no_args_is_help=True,
)

# Keys that GlobalConfig understands, with their expected Python types.
_KNOWN_KEYS: dict[str, type] = {
    "default_model": str,
    "max_turns": int,
    "max_cost": float,
}


@sub.command("get")
def config_get() -> None:
    """Print the current global configuration as a TOML-style table."""
    cfg = load_global_config()
    table = Table(show_header=True, header_style="bold")
    table.add_column("key")
    table.add_column("value")

    for field in GlobalConfig.model_fields:
        val = getattr(cfg, field)
        table.add_row(field, str(val) if val is not None else "[dim]—[/dim]")

    config_file = paths.config_path()
    output.info(f"config file: {config_file}")
    output.print_table(table)


@sub.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key to set (e.g. max_turns)."),
    value: str = typer.Argument(..., help="New value for the key."),
) -> None:
    """Set a key in the global config file, creating it if needed.

    Supported keys: default_model (str), max_turns (int), max_cost (float).
    """
    if key not in _KNOWN_KEYS:
        known = ", ".join(sorted(_KNOWN_KEYS))
        output.die(f"Unknown config key {key!r}. Known keys: {known}")

    # Coerce value to the expected Python type.
    target_type = _KNOWN_KEYS[key]
    try:
        coerced: str | int | float = target_type(value)
    except (ValueError, TypeError) as e:
        output.die(f"Cannot convert {value!r} to {target_type.__name__}: {e}")

    config_file = paths.config_path()

    # Read existing data (tolerant of missing file).
    if config_file.exists():
        try:
            data: dict[str, object] = tomllib.loads(config_file.read_text())
        except Exception as e:
            output.die(f"Could not parse existing config.toml: {e}")
    else:
        data = {}

    data[key] = coerced

    # Atomic write: tmp + replace.
    tmp = config_file.with_name(config_file.name + ".tmp")
    try:
        tmp.write_text(_dump_toml(data))
        import os
        os.replace(tmp, config_file)
    except OSError as e:
        output.die(f"Could not write config.toml: {e}")

    output.success(f"Set {key} = {coerced!r} in {config_file}")
