"""CLI commands for the roster: hire, fire, roster, show."""

from __future__ import annotations

import typer
from rich.panel import Panel
from rich.table import Table

from workforce import output, paths
from workforce.specialist import (
    DEFAULT_MODEL,
    TEMPLATES,
    RosterError,
    RosterStore,
    Specialist,
)


def _store() -> RosterStore:
    paths.ensure_layout()
    return RosterStore()


def hire(
    name: str = typer.Argument(..., help="Specialist name (lowercase id)."),
    role: str | None = typer.Option(
        None,
        "--role",
        help="Role description. Required unless --from-template provides one.",
    ),
    model: str | None = typer.Option(
        None,
        "--model",
        help=f"Claude model id (default: {DEFAULT_MODEL}).",
    ),
    from_template: str | None = typer.Option(
        None,
        "--from-template",
        help=f"Template to seed from. One of: {', '.join(sorted(TEMPLATES))}.",
    ),
) -> None:
    """Hire a new specialist into the roster."""
    if from_template is None and role is None:
        output.die("hire: provide --role or --from-template (or both).")

    store = _store()
    try:
        if from_template is not None:
            spec = Specialist.from_template(
                name, from_template, role=role, model=model
            )
        else:
            assert role is not None
            spec = Specialist.custom(name, role=role, model=model)
        store.save(spec)
    except (RosterError, ValueError) as e:
        output.die(str(e))

    role_preview = spec.role[:60] + ("..." if len(spec.role) > 60 else "")
    output.success(
        f"hired {spec.name} ({spec.model}) — "
        f"{len(spec.allowed_tools)} tools, role: {role_preview}"
    )


def fire(
    name: str = typer.Argument(..., help="Specialist name."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompt."
    ),
) -> None:
    """Remove a specialist from the roster (deletes their memory and stats)."""
    store = _store()
    if not store.exists(name):
        output.die(f"no such specialist: {name!r}")

    if not yes:
        confirm = typer.confirm(
            f"Delete specialist {name!r} including their memory and stats?",
            default=False,
        )
        if not confirm:
            output.info("aborted")
            raise typer.Exit()

    try:
        store.delete(name)
    except RosterError as e:
        output.die(str(e))
    output.success(f"fired {name}")


def roster() -> None:
    """List all specialists."""
    store = _store()
    names = store.names()
    if not names:
        output.info("roster is empty — try `workforce hire <name> --from-template <tmpl>`")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("model")
    table.add_column("missions", justify="right")
    table.add_column("cost (usd)", justify="right")
    table.add_column("role", overflow="fold")

    for name in names:
        spec = store.load(name)
        stats = store.load_stats(name)
        total = stats.missions_completed + stats.missions_failed
        table.add_row(
            spec.name,
            spec.model,
            str(total),
            f"{stats.total_cost_usd:.2f}",
            spec.role,
        )

    output.print_table(table)


def show(name: str = typer.Argument(..., help="Specialist name.")) -> None:
    """Show one specialist including their memory."""
    store = _store()
    try:
        spec = store.load(name)
    except RosterError as e:
        output.die(str(e))

    stats = store.load_stats(name)
    memory = store.load_memory(name).strip()

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="bold")
    meta.add_column()
    meta.add_row("name", spec.name)
    meta.add_row("model", spec.model)
    meta.add_row("role", spec.role)
    meta.add_row("tools", ", ".join(spec.allowed_tools))
    meta.add_row("missions completed", str(stats.missions_completed))
    meta.add_row("missions failed", str(stats.missions_failed))
    meta.add_row("total cost (usd)", f"{stats.total_cost_usd:.4f}")
    output.raw(Panel(meta, title=f"specialist: {name}", title_align="left"))

    output.raw(Panel(spec.base_prompt.rstrip(), title="base prompt", title_align="left"))

    output.raw(
        Panel(
            memory or "[dim](no memory yet)[/dim]",
            title="cross-project memory",
            title_align="left",
        )
    )
