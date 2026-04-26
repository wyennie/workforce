"""Stack templates for `workforce init`.

A stack template bundles a curated set of specialist roles, optional project
config defaults, and WORKFORCE.md hint strings for common project archetypes.
Users can start from one of these templates and customise from there.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class StackTemplate:
    """Seed values for a project initialised from a named stack.

    Attributes:
        specialists: List of template keys (keys from ``specialist.TEMPLATES``)
            to hire when the stack is applied.
        specialist_names: Display names to hire each specialist under.  Must be
            the same length as ``specialists``.
        project_config_defaults: Key/value pairs written verbatim into the
            project's ``.workforce.toml`` configuration file.
        workforce_md_hints: Short strings surfaced as ``<!-- Hint: X -->``
            comment lines in the generated WORKFORCE.md to remind users what
            to fill in.
        review: Whether to include a ``reviewer`` specialist in the stack.
    """

    specialists: list[str]
    specialist_names: list[str]
    project_config_defaults: dict[str, object] = field(default_factory=dict)
    workforce_md_hints: list[str] = field(default_factory=list)
    review: bool = False


STACK_TEMPLATES: dict[str, StackTemplate] = {
    "django-api": StackTemplate(
        specialists=["backend", "tester"],
        specialist_names=["backend", "tester"],
        project_config_defaults={
            "stack": "django-api",
        },
        workforce_md_hints=[
            "Django version",
            "Test runner command",
            "Database setup",
        ],
        review=True,
    ),
    "fastapi": StackTemplate(
        specialists=["backend", "tester"],
        specialist_names=["backend", "tester"],
        project_config_defaults={
            "stack": "fastapi",
        },
        workforce_md_hints=[
            "FastAPI version",
            "Async or sync",
            "Database ORM",
        ],
        review=True,
    ),
    "react-app": StackTemplate(
        specialists=["frontend", "tester"],
        specialist_names=["frontend", "tester"],
        project_config_defaults={
            "stack": "react-app",
        },
        workforce_md_hints=[
            "Node version",
            "Build command",
            "Test framework",
        ],
    ),
    "next-js": StackTemplate(
        specialists=["frontend", "tester"],
        specialist_names=["frontend", "tester"],
        project_config_defaults={
            "stack": "next-js",
        },
        workforce_md_hints=[
            "Next.js version",
            "Deployment target (Vercel/self-hosted)",
            "API routes or separate backend",
        ],
    ),
    "monorepo": StackTemplate(
        specialists=["backend", "frontend", "tester"],
        specialist_names=["backend", "frontend", "tester"],
        project_config_defaults={
            "stack": "monorepo",
        },
        workforce_md_hints=[
            "Package manager (pnpm/yarn)",
            "Workspace structure",
            "Shared packages",
        ],
    ),
    "data-pipeline": StackTemplate(
        specialists=["data", "tester"],
        specialist_names=["data", "tester"],
        project_config_defaults={
            "stack": "data-pipeline",
        },
        workforce_md_hints=[
            "Data sources",
            "Pipeline orchestrator (Airflow/Prefect/dbt)",
            "Output formats",
        ],
    ),
    "cli-tool": StackTemplate(
        specialists=["backend", "tester"],
        specialist_names=["backend", "tester"],
        project_config_defaults={
            "stack": "cli-tool",
        },
        workforce_md_hints=[
            "Python version",
            "Entry point",
            "Distribution method (pip/brew/binary)",
        ],
    ),
}
