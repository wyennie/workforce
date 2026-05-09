"""`workforce init` — scaffold a new project with stack templates."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import typer
from rich.table import Table

from workforce import output, paths
from workforce import project as project_mod
from workforce.specialist import RosterStore, Specialist
from workforce.stacks import STACK_TEMPLATES
from workforce.utils import _dump_toml


def _generate_workforce_md(
    project_name: str,
    specialists: list[Specialist],
    hints: list[str],
) -> str:
    """Generate the content for a WORKFORCE.md file.

    Args:
        project_name: Display name of the project.
        specialists: Specialists hired for this project.
        hints: Hint strings to embed as comments in the Project Notes section.

    Returns:
        The full WORKFORCE.md content as a string.
    """
    lines: list[str] = []

    lines.append("# WORKFORCE.md")
    lines.append("")
    lines.append(
        "This file is read by the Workforce Manager before decomposing any ticket. "
        "Fill in the sections below to give the Manager context about the project."
    )
    lines.append("")

    # Specialist roster table
    lines.append("## Specialist Roster")
    lines.append("")
    lines.append("| Name | Role |")
    lines.append("|------|------|")
    for spec in specialists:
        lines.append(f"| `{spec.name}` | {spec.role} |")
    if not specialists:
        lines.append("| *(none hired yet)* | |")
    lines.append("")

    # Common tickets
    lines.append("## Common Tickets")
    lines.append("")
    lines.append("<!-- Describe recurring ticket types here so the Manager can anticipate patterns. -->")
    lines.append("<!-- Example: 'Add a REST endpoint for <resource>' or 'Write tests for <module>' -->")
    lines.append("")

    # Project notes + hints
    lines.append("## Project Notes")
    lines.append("")
    lines.append("<!-- Fill in project-specific context below. -->")
    for hint in hints:
        lines.append(f"<!-- Hint: {hint} -->")
    lines.append("")

    # Build & test
    lines.append("## Build & Test")
    lines.append("")
    lines.append("<!-- How to install dependencies, run the test suite, and lint. -->")
    lines.append("<!-- Example:")
    lines.append("uv pip install -e '.[dev]'")
    lines.append("pytest tests/ -x -q")
    lines.append("-->")
    lines.append("")

    # Deployment
    lines.append("## Deployment")
    lines.append("")
    lines.append("<!-- How to build and deploy the project (CI pipeline, platform, etc.). -->")
    lines.append("")

    return "\n".join(lines)


def _write_workforce_toml(repo_path: Path, defaults: dict[str, object]) -> None:
    """Write ``.workforce.toml`` into *repo_path* with *defaults*."""
    content = _dump_toml(defaults)
    (repo_path / ".workforce.toml").write_text(content)


def _run_git(args: list[str], cwd: Path) -> None:
    """Run a git command; raises CalledProcessError on failure."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def init_command(
    template: str | None = typer.Option(
        None, "--template", "-t",
        help="Stack template to apply (use --list to see options).",
        metavar="NAME",
    ),
    blank: bool = typer.Option(
        False, "--blank",
        help="Register the project with no specialists.",
    ),
    list_templates: bool = typer.Option(
        False, "--list",
        help="Print available stack templates and exit.",
    ),
    name: str | None = typer.Option(
        None, "--name",
        help="Project display name (default: directory basename).",
    ),
    demo: bool = typer.Option(
        False, "--demo",
        help=(
            "Create a toy calculator project in a temp directory, "
            "hire a tester, and write a starter ticket."
        ),
    ),
) -> None:
    """Scaffold a new Workforce project in the current directory.

    Registers the current directory as a project, optionally hires specialists
    from a stack template, and writes WORKFORCE.md and .workforce.toml.

    Examples::

        workforce init --template fastapi
        workforce init --template react-app --name my-app
        workforce init --blank
        workforce init --list
        workforce init --demo
    """
    # --list: show available templates and exit
    if list_templates:
        table = Table(show_header=True, header_style="bold")
        table.add_column("template")
        table.add_column("specialists")
        table.add_column("hints")

        for tname, tmpl in sorted(STACK_TEMPLATES.items()):
            specialists_str = ", ".join(tmpl.specialists)
            hints_str = "; ".join(tmpl.workforce_md_hints[:2])
            if len(tmpl.workforce_md_hints) > 2:
                hints_str += f" (+{len(tmpl.workforce_md_hints) - 2} more)"
            table.add_row(tname, specialists_str, hints_str)

        output.print_table(table)
        raise typer.Exit()

    # --demo mode
    if demo:
        _run_demo()
        return

    if template and blank:
        output.die("--template and --blank are mutually exclusive")

    if template and template not in STACK_TEMPLATES:
        available = ", ".join(sorted(STACK_TEMPLATES))
        output.die(
            f"unknown template {template!r}; available: {available}\n"
            "Use --list to see all templates with details."
        )

    paths.ensure_layout()

    repo_path = Path.cwd()

    # Check not already registered
    existing_marker = project_mod.read_marker(repo_path)
    if existing_marker:
        pstore = project_mod.ProjectStore()
        if pstore.exists(existing_marker):
            output.die(
                f"this directory is already registered as project "
                f"{existing_marker!r}. Use `workforce project show {existing_marker}` "
                "to inspect it."
            )

    # Determine project kind
    has_git = project_mod.is_git_repo(repo_path)
    kind = "repo" if has_git else "workspace"

    # Resolve project ID and display name
    try:
        project_id = project_mod.resolve_project_id(repo_path)
    except project_mod.ProjectError as e:
        output.die(str(e))

    display_name = name or repo_path.name
    try:
        proj = project_mod.Project(
            id=project_id,
            name=display_name,
            repo_path=str(repo_path),
            kind=kind,
        )
    except ValueError as e:
        output.die(str(e))

    pstore = project_mod.ProjectStore()
    try:
        pstore.save(proj)
    except project_mod.ProjectError as e:
        output.die(str(e))

    # Write marker into repo
    try:
        project_mod.write_marker(repo_path, project_id)
    except OSError as e:
        output.warn(
            f"could not write {project_mod.MARKER_FILENAME} marker: {e}. "
            "If you move the directory, the project id will change."
        )

    # Hire specialists and collect them for WORKFORCE.md
    rstore = RosterStore()
    hired: list[Specialist] = []
    hints: list[str] = []

    if template:
        stack = STACK_TEMPLATES[template]
        hints = list(stack.workforce_md_hints)

        # Write .workforce.toml
        if stack.project_config_defaults:
            _write_workforce_toml(repo_path, stack.project_config_defaults)
            output.success(f"wrote .workforce.toml with stack={template!r}")

        for spec_name, template_key in zip(stack.specialist_names, stack.specialists, strict=False):
            if rstore.exists(spec_name):
                # Specialist already in global roster — reuse and assign
                spec = rstore.load(spec_name)
                output.info(f"  reusing existing specialist {spec_name!r}")
            else:
                spec = Specialist.from_template(spec_name, template_key)
                rstore.save(spec)
                output.success(f"  hired {spec_name!r} from {template_key!r} template")

            if spec_name not in proj.assigned_specialists:
                proj.assigned_specialists.append(spec_name)
            hired.append(spec)

        # Optionally hire a reviewer
        if stack.review:
            rev_name = "reviewer"
            if rstore.exists(rev_name):
                rev = rstore.load(rev_name)
                output.info(f"  reusing existing specialist {rev_name!r}")
            else:
                rev = Specialist.from_template(rev_name, "reviewer")
                rstore.save(rev)
                output.success(f"  hired {rev_name!r} from 'reviewer' template")

            if rev_name not in proj.assigned_specialists:
                proj.assigned_specialists.append(rev_name)
            hired.append(rev)

        pstore.save(proj, overwrite=True)

    # Generate and write WORKFORCE.md
    wf_md = _generate_workforce_md(display_name, hired, hints)
    wf_md_path = repo_path / "WORKFORCE.md"
    wf_md_path.write_text(wf_md)
    output.success("wrote WORKFORCE.md")

    # Print success summary
    output.rule()
    table = Table(show_header=True, header_style="bold")
    table.add_column("item")
    table.add_column("value")
    table.add_row("project", display_name)
    table.add_row("id", project_id)
    table.add_row("kind", kind)
    table.add_row("template", template or "(blank)")
    table.add_row("specialists", ", ".join(proj.assigned_specialists) or "(none)")
    output.print_table(table)
    output.rule()

    output.info("[bold]Next steps:[/bold]")
    output.info("  1. Fill in WORKFORCE.md with project context")
    if template:
        output.info("  2. Update .workforce.toml with stack-specific config")
    dispatch_example = proj.assigned_specialists[0] if proj.assigned_specialists else "<specialist>"
    output.info(
        f"  {'3' if template else '2'}. Dispatch a mission: "
        f"`workforce dispatch {display_name!r} --specialist {dispatch_example} "
        f"'your ticket here'`"
    )


def _run_demo() -> None:
    """Create a toy calculator demo project and print onboarding instructions."""
    tmp = tempfile.mkdtemp(prefix="workforce-demo-")
    demo_dir = Path(tmp)

    # Write a simple calculator module
    (demo_dir / "calculator.py").write_text(
        '"""A simple calculator module."""\n\n\n'
        "def add(a: float, b: float) -> float:\n"
        '    """Return a + b."""\n'
        "    return a + b\n\n\n"
        "def subtract(a: float, b: float) -> float:\n"
        '    """Return a - b."""\n'
        "    return a - b\n\n\n"
        "def multiply(a: float, b: float) -> float:\n"
        '    """Return a * b."""\n'
        "    return a * b\n\n\n"
        "def divide(a: float, b: float) -> float:\n"
        '    """Return a / b.  Raises ZeroDivisionError if b is 0."""\n'
        "    if b == 0:\n"
        '        raise ZeroDivisionError("cannot divide by zero")\n'
        "    return a / b\n"
    )

    # Write a demo ticket
    (demo_dir / "demo-ticket.md").write_text(
        "# Demo Ticket\n\n"
        "Add pytest tests for `calculator.py`.\n\n"
        "## Requirements\n\n"
        "- Test each function (add, subtract, multiply, divide)\n"
        "- Include edge cases: zero inputs, negative numbers\n"
        "- Test that `divide` raises `ZeroDivisionError` when divisor is 0\n"
        "- Aim for 100% coverage on `calculator.py`\n"
    )

    # Init git repo
    _run_git(["init", "-q", "-b", "main"], demo_dir)
    _run_git(["config", "user.email", "demo@workforce.local"], demo_dir)
    _run_git(["config", "user.name", "Workforce Demo"], demo_dir)
    _run_git(["add", "calculator.py", "demo-ticket.md"], demo_dir)
    _run_git(["commit", "-q", "-m", "chore: initial demo project"], demo_dir)

    # Register as a project
    paths.ensure_layout()
    project_id = project_mod.compute_project_id(demo_dir)
    proj = project_mod.Project(
        id=project_id,
        name="calculator-demo",
        repo_path=str(demo_dir),
        kind="repo",
    )
    pstore = project_mod.ProjectStore()
    try:
        pstore.save(proj)
    except project_mod.ProjectError as e:
        output.die(f"demo setup failed: {e}")

    try:
        project_mod.write_marker(demo_dir, project_id)
    except OSError:
        pass

    # Hire a tester
    rstore = RosterStore()
    tester_name = "tester"
    if not rstore.exists(tester_name):
        tester = Specialist.from_template(tester_name, "tester")
        rstore.save(tester)
    else:
        tester = rstore.load(tester_name)

    proj.assigned_specialists.append(tester_name)
    pstore.save(proj, overwrite=True)

    # Write WORKFORCE.md
    wf_md = _generate_workforce_md("calculator-demo", [tester], [])
    (demo_dir / "WORKFORCE.md").write_text(wf_md)

    output.success(f"demo project created at {demo_dir}")
    output.info("  calculator.py  — module with add/subtract/multiply/divide")
    output.info("  demo-ticket.md — starter ticket: add pytest tests")
    output.info("")
    output.info("[bold]Run this to start:[/bold]")
    output.info(
        f"  workforce dispatch calculator-demo "
        f"--specialist tester "
        f"\"$(cat {demo_dir}/demo-ticket.md)\""
    )
