"""Tests for `workforce memory` subcommands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from workforce.cli import app
from workforce.project import Project, ProjectStore
from workforce.specialist import RosterStore, Specialist


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WORKFORCE_HOME", str(home))
    return home


@pytest.fixture
def specialist_with_memory(isolated_home: Path) -> tuple[Specialist, Path]:
    """Hire a specialist and pre-populate their cross-project memory."""
    isolated_home.mkdir(parents=True, exist_ok=True)
    store = RosterStore()
    spec = Specialist.from_template("aria", "backend")
    store.save(spec)
    mem_path = isolated_home / "roster" / "aria" / "memory.md"
    mem_path.write_text(
        "## m-001\n\nAlways test edge cases.\n\n"
        "## m-002\n\nPrefer boring solutions over clever ones.\n"
    )
    return spec, mem_path


@pytest.fixture
def project_with_memory(
    specialist_with_memory: tuple[Specialist, Path],
) -> tuple[Project, Path]:
    """Register a project and give aria per-project memory."""
    spec, _ = specialist_with_memory
    pstore = ProjectStore()
    proj = Project(
        id="abc123def456",
        name="myapp",
        repo_path="/tmp/myapp",
        kind="workspace",
        assigned_specialists=[spec.name],
    )
    pstore.save(proj)
    mem_dir = pstore.memory_dir(proj.id)
    mem_path = mem_dir / f"{spec.name}.md"
    mem_path.write_text("## project-001\n\nUse postgres for persistence.\n")
    return proj, mem_path


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_memory_show_lists_files(
    specialist_with_memory: tuple[Specialist, Path],
) -> None:
    """show prints a table row for the cross-project memory file."""
    spec, mem_path = specialist_with_memory
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "show", "aria"])
    assert result.exit_code == 0, result.output
    assert "cross-project" in result.output
    assert "aria" in result.output


def test_memory_show_unknown_specialist(isolated_home: Path) -> None:
    """show exits 1 for an unknown specialist."""
    isolated_home.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "show", "nonexistent"])
    assert result.exit_code != 0


def test_memory_show_includes_project_rows(
    project_with_memory: tuple[Project, Path],
) -> None:
    """show lists per-project memory rows when specialist is assigned."""
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "show", "aria"])
    assert result.exit_code == 0, result.output
    assert "myapp" in result.output


def test_memory_show_token_count(
    specialist_with_memory: tuple[Specialist, Path],
) -> None:
    """Token count shown is len(text) // 4."""
    spec, mem_path = specialist_with_memory
    text = mem_path.read_text()
    expected_tokens = len(text) // 4
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "show", "aria"])
    assert result.exit_code == 0, result.output
    assert str(expected_tokens) in result.output


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_memory_search_finds_match(
    specialist_with_memory: tuple[Specialist, Path],
) -> None:
    """search finds a line that matches the query."""
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "search", "aria", "edge cases"])
    assert result.exit_code == 0, result.output
    assert "edge cases" in result.output


def test_memory_search_case_insensitive(
    specialist_with_memory: tuple[Specialist, Path],
) -> None:
    """search is case-insensitive."""
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "search", "aria", "EDGE CASES"])
    assert result.exit_code == 0, result.output
    assert "edge cases" in result.output.lower()


def test_memory_search_no_match(
    specialist_with_memory: tuple[Specialist, Path],
) -> None:
    """search prints 'no matches' when nothing is found."""
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "search", "aria", "xyzzy_no_match_9999"])
    assert result.exit_code == 0, result.output
    assert "no matches" in result.output.lower()


def test_memory_search_shows_context(
    specialist_with_memory: tuple[Specialist, Path],
) -> None:
    """search prints surrounding context lines."""
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "search", "aria", "boring"])
    assert result.exit_code == 0, result.output
    # The adjacent line (## m-002) should appear as context.
    assert "m-002" in result.output


def test_memory_search_with_project(
    project_with_memory: tuple[Project, Path],
) -> None:
    """search also searches per-project memory when --project is given."""
    runner = CliRunner()
    result = runner.invoke(
        app, ["memory", "search", "aria", "postgres", "--project", "myapp"]
    )
    assert result.exit_code == 0, result.output
    assert "postgres" in result.output


def test_memory_search_unknown_specialist(isolated_home: Path) -> None:
    """search exits 1 for an unknown specialist."""
    isolated_home.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "search", "ghost", "anything"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def test_memory_export_prints_cross_project_header(
    specialist_with_memory: tuple[Specialist, Path],
) -> None:
    """export includes the '# Cross-project memory' header."""
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "export", "aria"])
    assert result.exit_code == 0, result.output
    assert "# Cross-project memory" in result.output


def test_memory_export_includes_content(
    specialist_with_memory: tuple[Specialist, Path],
) -> None:
    """export prints the memory content."""
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "export", "aria"])
    assert result.exit_code == 0, result.output
    assert "edge cases" in result.output


def test_memory_export_with_project(
    project_with_memory: tuple[Project, Path],
) -> None:
    """export with --project also prints the project memory section."""
    runner = CliRunner()
    result = runner.invoke(app, ["memory", "export", "aria", "--project", "myapp"])
    assert result.exit_code == 0, result.output
    assert "# Cross-project memory" in result.output
    assert "# Project memory: myapp" in result.output
    assert "postgres" in result.output


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def test_memory_import_replaces_cross_project(
    specialist_with_memory: tuple[Specialist, Path],
    tmp_path: Path,
) -> None:
    """import replaces the cross-project memory with file contents."""
    src = tmp_path / "new_memory.md"
    src.write_text("## imported\n\nNew lesson learned.\n")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["memory", "import", "aria", "--file", str(src), "--yes"],
    )
    assert result.exit_code == 0, result.output

    _, mem_path = specialist_with_memory
    assert "New lesson learned." in mem_path.read_text()


def test_memory_import_replaces_project_memory(
    project_with_memory: tuple[Project, Path],
    tmp_path: Path,
) -> None:
    """import --project replaces the per-project memory."""
    src = tmp_path / "proj_mem.md"
    src.write_text("## new-project-lesson\n\nUse redis for caching.\n")

    proj, mem_path = project_with_memory
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "memory", "import", "aria",
            "--file", str(src),
            "--project", "myapp",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "redis" in mem_path.read_text()


def test_memory_import_aborts_without_yes(
    specialist_with_memory: tuple[Specialist, Path],
    tmp_path: Path,
) -> None:
    """import prompts for confirmation and aborts on 'n'."""
    src = tmp_path / "mem.md"
    src.write_text("replacement content\n")

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["memory", "import", "aria", "--file", str(src)],
        input="n\n",
    )
    assert result.exit_code == 0, result.output

    _, mem_path = specialist_with_memory
    # Original content unchanged.
    assert "replacement content" not in mem_path.read_text()
