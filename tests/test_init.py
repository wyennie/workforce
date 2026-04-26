"""Tests for `workforce init` command and supporting modules."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from workforce.cli import app
from workforce.cli.init import _generate_workforce_md
from workforce.project import ProjectStore
from workforce.specialist import RosterStore, Specialist
from workforce.stacks import STACK_TEMPLATES, StackTemplate


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WORKFORCE_HOME", str(home))
    return home


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=r, check=True)
    (r / "README.md").write_text("# t\n")
    subprocess.run(["git", "add", "README.md"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=r, check=True)
    return r


# ---------------------------------------------------------------------------
# stacks.py unit tests
# ---------------------------------------------------------------------------


def test_stack_templates_all_defined() -> None:
    """All 7 expected stack templates must exist."""
    expected = {
        "django-api", "fastapi", "react-app", "next-js",
        "monorepo", "data-pipeline", "cli-tool",
    }
    assert expected == set(STACK_TEMPLATES)


def test_stack_template_is_frozen() -> None:
    """StackTemplate instances must be immutable (frozen dataclass)."""
    tmpl = STACK_TEMPLATES["fastapi"]
    with pytest.raises((AttributeError, TypeError)):
        tmpl.review = False  # type: ignore[misc]


def test_django_api_has_review() -> None:
    assert STACK_TEMPLATES["django-api"].review is True


def test_fastapi_has_review() -> None:
    assert STACK_TEMPLATES["fastapi"].review is True


def test_react_app_no_review() -> None:
    assert STACK_TEMPLATES["react-app"].review is False


def test_all_specialist_keys_exist_in_templates() -> None:
    """Every specialist key referenced by a stack must exist in TEMPLATES."""
    from workforce.specialist import TEMPLATES

    all_keys: set[str] = set()
    for stack in STACK_TEMPLATES.values():
        all_keys.update(stack.specialists)

    missing = all_keys - set(TEMPLATES)
    assert not missing, f"Stack references unknown specialist template keys: {missing}"


def test_stack_specialist_names_length_matches() -> None:
    for name, stack in STACK_TEMPLATES.items():
        assert len(stack.specialists) == len(stack.specialist_names), (
            f"{name}: specialists and specialist_names must have the same length"
        )


# ---------------------------------------------------------------------------
# _generate_workforce_md unit tests
# ---------------------------------------------------------------------------


def test_generate_workforce_md_includes_specialists() -> None:
    spec = Specialist.from_template("backend", "backend")
    content = _generate_workforce_md("myapp", [spec], [])
    assert "## Specialist Roster" in content
    assert "backend" in content


def test_generate_workforce_md_includes_hints() -> None:
    content = _generate_workforce_md("myapp", [], ["Django version", "Test runner"])
    assert "<!-- Hint: Django version -->" in content
    assert "<!-- Hint: Test runner -->" in content


def test_generate_workforce_md_sections_present() -> None:
    content = _generate_workforce_md("demo", [], [])
    for section in ["## Specialist Roster", "## Common Tickets",
                    "## Project Notes", "## Build & Test", "## Deployment"]:
        assert section in content, f"missing section: {section}"


def test_generate_workforce_md_no_specialists() -> None:
    content = _generate_workforce_md("empty", [], [])
    assert "*(none hired yet)*" in content


# ---------------------------------------------------------------------------
# `workforce init` CLI tests
# ---------------------------------------------------------------------------


def test_init_list_exits_zero(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--list"])
    assert result.exit_code == 0, result.output
    # All 7 template names should appear in the output
    for name in STACK_TEMPLATES:
        assert name in result.output


def test_init_blank_registers_project(
    isolated_home: Path,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(git_repo)
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--blank", "--name", "myapp"])
    assert result.exit_code == 0, result.output

    pstore = ProjectStore()
    proj = pstore.resolve("myapp")
    assert proj.repo_path == str(git_repo)
    assert proj.kind == "repo"
    assert proj.assigned_specialists == []


def test_init_blank_writes_workforce_md(
    isolated_home: Path,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(git_repo)
    runner = CliRunner()
    runner.invoke(app, ["init", "--blank", "--name", "myapp"])
    wf_md = git_repo / "WORKFORCE.md"
    assert wf_md.is_file()
    content = wf_md.read_text()
    assert "# WORKFORCE.md" in content
    assert "## Specialist Roster" in content


def test_init_template_hires_specialists(
    isolated_home: Path,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(git_repo)
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--template", "fastapi", "--name", "myapi"])
    assert result.exit_code == 0, result.output

    pstore = ProjectStore()
    proj = pstore.resolve("myapi")
    # fastapi stack: backend, tester, reviewer (review=True)
    assert "backend" in proj.assigned_specialists
    assert "tester" in proj.assigned_specialists
    assert "reviewer" in proj.assigned_specialists

    rstore = RosterStore()
    for name in ("backend", "tester", "reviewer"):
        assert rstore.exists(name), f"specialist {name!r} not in roster"


def test_init_template_writes_workforce_toml(
    isolated_home: Path,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(git_repo)
    runner = CliRunner()
    runner.invoke(app, ["init", "--template", "fastapi", "--name", "myapi"])
    toml_path = git_repo / ".workforce.toml"
    assert toml_path.is_file()
    content = toml_path.read_text()
    assert "fastapi" in content


def test_init_template_writes_hints_in_workforce_md(
    isolated_home: Path,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(git_repo)
    runner = CliRunner()
    runner.invoke(app, ["init", "--template", "fastapi", "--name", "myapi"])
    content = (git_repo / "WORKFORCE.md").read_text()
    for hint in STACK_TEMPLATES["fastapi"].workforce_md_hints:
        assert hint in content


def test_init_data_pipeline_template(
    isolated_home: Path,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(git_repo)
    runner = CliRunner()
    result = runner.invoke(
        app, ["init", "--template", "data-pipeline", "--name", "mydata"]
    )
    assert result.exit_code == 0, result.output

    pstore = ProjectStore()
    proj = pstore.resolve("mydata")
    assert "data" in proj.assigned_specialists
    assert "tester" in proj.assigned_specialists


def test_init_unknown_template_exits_nonzero(
    isolated_home: Path,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(git_repo)
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--template", "nonexistent"])
    assert result.exit_code != 0


def test_init_template_and_blank_mutually_exclusive(
    isolated_home: Path,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(git_repo)
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--template", "fastapi", "--blank"])
    assert result.exit_code != 0


def test_init_second_run_same_dir_fails(
    isolated_home: Path,
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registering the same directory twice should exit non-zero."""
    monkeypatch.chdir(git_repo)
    runner = CliRunner()
    runner.invoke(app, ["init", "--blank", "--name", "first-run"])
    result = runner.invoke(app, ["init", "--blank", "--name", "second-run"])
    assert result.exit_code != 0


def test_init_demo(
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["init", "--demo"])
    assert result.exit_code == 0, result.output

    pstore = ProjectStore()
    proj = pstore.resolve("calculator-demo")
    assert proj.kind == "repo"

    demo_dir = Path(proj.repo_path)
    assert (demo_dir / "calculator.py").is_file()
    assert (demo_dir / "demo-ticket.md").is_file()
    assert (demo_dir / "WORKFORCE.md").is_file()

    rstore = RosterStore()
    assert rstore.exists("tester")
