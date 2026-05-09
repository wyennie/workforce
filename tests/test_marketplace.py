"""Tests for the specialist marketplace CLI commands (install, publish, search)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from workforce.cli import app
from workforce.specialist import RosterStore, Specialist

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate WORKFORCE_HOME to a temp directory."""
    home = tmp_path / "home"
    monkeypatch.setenv("WORKFORCE_HOME", str(home))
    return home


@pytest.fixture
def store(isolated_home: Path) -> RosterStore:
    """Return a RosterStore backed by the isolated home."""
    from workforce import paths

    paths.ensure_layout()
    return RosterStore()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SPECIALIST_TOML = """\
schema_version = 1
name = "myspec"
role = "A test specialist."
model = "claude-sonnet-4-6"
allowed_tools = ["Read", "Write", "Edit", "Bash"]

base_prompt = '''
## Role

A test specialist.
'''
"""

_INDEX = [
    {
        "slug": "backend-go",
        "description": "Go backend engineer.",
        "templates_used": ["backend"],
        "author": "alice",
    },
    {
        "slug": "frontend-react",
        "description": "React frontend specialist.",
        "templates_used": ["frontend"],
        "author": "bob",
    },
]


def _urlopen_mock(content: bytes) -> MagicMock:
    """Return a context-manager mock that yields a response with *content*."""
    resp = MagicMock()
    resp.read.return_value = content
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_with_explicit_name(isolated_home: Path) -> None:
    """--name bypasses the interactive prompt and installs under that name."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(_SPECIALIST_TOML.encode())):
        result = runner.invoke(
            app,
            ["specialist", "install", "myspec", "--name", "local-name"],
        )

    assert result.exit_code == 0, result.output
    assert "installed specialist" in result.output
    assert RosterStore().exists("local-name")


def test_install_default_name_from_prompt(isolated_home: Path) -> None:
    """Pressing Enter at the name prompt accepts the slug as the default."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(_SPECIALIST_TOML.encode())):
        # '\n' accepts the default (slug = "myspec")
        result = runner.invoke(
            app,
            ["specialist", "install", "myspec"],
            input="\n",
        )

    assert result.exit_code == 0, result.output
    assert RosterStore().exists("myspec")


def test_install_custom_name_from_prompt(isolated_home: Path) -> None:
    """Typing a custom name at the prompt installs under that name."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(_SPECIALIST_TOML.encode())):
        result = runner.invoke(
            app,
            ["specialist", "install", "myspec"],
            input="custom-name\n",
        )

    assert result.exit_code == 0, result.output
    assert RosterStore().exists("custom-name")
    assert not RosterStore().exists("myspec")


def test_install_network_error_exits_nonzero(isolated_home: Path) -> None:
    """A URLError produces a non-zero exit code."""
    import urllib.error

    runner = CliRunner()
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no internet")):
        result = runner.invoke(app, ["specialist", "install", "missing-slug"])

    assert result.exit_code != 0


def test_install_overwrite_confirmed(isolated_home: Path, store: RosterStore) -> None:
    """When specialist already exists and user confirms, it is replaced."""
    existing = Specialist.custom("myspec", role="old role")
    store.save(existing)

    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(_SPECIALIST_TOML.encode())):
        result = runner.invoke(
            app,
            ["specialist", "install", "myspec", "--name", "myspec"],
            input="y\n",  # confirm overwrite
        )

    assert result.exit_code == 0, result.output
    reloaded = store.load("myspec")
    assert reloaded.role == "A test specialist."


def test_install_overwrite_declined(isolated_home: Path, store: RosterStore) -> None:
    """When specialist already exists and user declines, it is not replaced."""
    original = Specialist.custom("myspec", role="old role")
    store.save(original)

    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(_SPECIALIST_TOML.encode())):
        result = runner.invoke(
            app,
            ["specialist", "install", "myspec", "--name", "myspec"],
            input="n\n",  # decline overwrite
        )

    # Exit 0 because the user chose to abort (not an error condition).
    assert result.exit_code == 0
    # Role must be unchanged.
    assert store.load("myspec").role == "old role"


def test_install_invalid_local_name(isolated_home: Path) -> None:
    """An invalid local name produces a non-zero exit code."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(_SPECIALIST_TOML.encode())):
        result = runner.invoke(
            app,
            ["specialist", "install", "myspec", "--name", "INVALID NAME!"],
        )

    assert result.exit_code != 0


def test_install_invalid_toml(isolated_home: Path) -> None:
    """Malformed TOML from the registry produces a non-zero exit code."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(b"this is not toml ][")):
        result = runner.invoke(
            app,
            ["specialist", "install", "myspec", "--name", "myspec"],
        )

    assert result.exit_code != 0


def test_install_hint_printed(isolated_home: Path) -> None:
    """Success output contains a hint about assigning to a project."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(_SPECIALIST_TOML.encode())):
        result = runner.invoke(
            app,
            ["specialist", "install", "myspec", "--name", "myspec"],
        )

    assert result.exit_code == 0, result.output
    assert "project assign" in result.output


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_all_entries(isolated_home: Path) -> None:
    """With no query, all index entries are listed."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(json.dumps(_INDEX).encode())):
        result = runner.invoke(app, ["specialist", "search"])

    assert result.exit_code == 0, result.output
    assert "backend-go" in result.output
    assert "frontend-react" in result.output


def test_search_filters_by_slug(isolated_home: Path) -> None:
    """Query matching a slug prefix filters correctly."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(json.dumps(_INDEX).encode())):
        result = runner.invoke(app, ["specialist", "search", "go"])

    assert result.exit_code == 0, result.output
    assert "backend-go" in result.output
    assert "frontend-react" not in result.output


def test_search_filters_by_description(isolated_home: Path) -> None:
    """Query matching description text filters correctly."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(json.dumps(_INDEX).encode())):
        result = runner.invoke(app, ["specialist", "search", "react"])

    assert result.exit_code == 0, result.output
    assert "frontend-react" in result.output
    assert "backend-go" not in result.output


def test_search_case_insensitive(isolated_home: Path) -> None:
    """Query matching is case-insensitive."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(json.dumps(_INDEX).encode())):
        result = runner.invoke(app, ["specialist", "search", "REACT"])

    assert result.exit_code == 0, result.output
    assert "frontend-react" in result.output


def test_search_no_match(isolated_home: Path) -> None:
    """A query that matches nothing produces a friendly message."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(json.dumps(_INDEX).encode())):
        result = runner.invoke(app, ["specialist", "search", "zzznomatch"])

    assert result.exit_code == 0, result.output
    assert "no specialists found" in result.output


def test_search_network_error_is_graceful(isolated_home: Path) -> None:
    """A URLError is printed as a warning, not a hard failure."""
    import urllib.error

    runner = CliRunner()
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
        result = runner.invoke(app, ["specialist", "search"])

    # Exit 0 — the user can recover (check internet, try again).
    assert result.exit_code == 0
    assert "could not reach" in result.output


def test_search_install_hint_printed(isolated_home: Path) -> None:
    """Successful search output contains an install hint."""
    runner = CliRunner()
    with patch("urllib.request.urlopen", return_value=_urlopen_mock(json.dumps(_INDEX).encode())):
        result = runner.invoke(app, ["specialist", "search"])

    assert "specialist install" in result.output


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


def test_publish_writes_specialist_toml(isolated_home: Path, store: RosterStore, tmp_path: Path) -> None:
    """Publish writes a valid specialist.toml to the output directory."""
    spec = Specialist.from_template("myspec", "backend")
    store.save(spec)

    output_dir = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["specialist", "publish", "myspec", "--output-dir", str(output_dir)],
    )

    assert result.exit_code == 0, result.output
    toml_path = output_dir / "specialist.toml"
    assert toml_path.is_file()

    with toml_path.open("rb") as f:
        data = tomllib.load(f)
    assert data["name"] == "myspec"
    assert data["role"] == spec.role


def test_publish_writes_readme(isolated_home: Path, store: RosterStore, tmp_path: Path) -> None:
    """Publish writes a README.md stub."""
    spec = Specialist.from_template("myspec", "backend")
    store.save(spec)

    output_dir = tmp_path / "out"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["specialist", "publish", "myspec", "--output-dir", str(output_dir)],
    )

    assert result.exit_code == 0, result.output
    readme = output_dir / "README.md"
    assert readme.is_file()
    content = readme.read_text()
    assert "myspec" in content
    assert "workforce specialist install" in content


def test_publish_default_output_dir(isolated_home: Path, store: RosterStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no --output-dir, files land in ./specialists/<name>/."""
    spec = Specialist.from_template("myspec", "backend")
    store.save(spec)

    # Change cwd so the default path is predictable.
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(app, ["specialist", "publish", "myspec"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "specialists" / "myspec" / "specialist.toml").is_file()
    assert (tmp_path / "specialists" / "myspec" / "README.md").is_file()


def test_publish_unknown_specialist(isolated_home: Path, tmp_path: Path) -> None:
    """Publishing a non-existent specialist exits with non-zero code."""
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["specialist", "publish", "no-such-specialist", "--output-dir", str(tmp_path)],
    )

    assert result.exit_code != 0


def test_publish_prints_pr_instructions(isolated_home: Path, store: RosterStore, tmp_path: Path) -> None:
    """Publish output contains instructions about opening a pull request."""
    spec = Specialist.from_template("myspec", "backend")
    store.save(spec)

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["specialist", "publish", "myspec", "--output-dir", str(tmp_path / "out")],
    )

    assert result.exit_code == 0, result.output
    assert "pull request" in result.output.lower()
    assert "workforce-ai/specialists" in result.output


def test_publish_does_not_include_memory(isolated_home: Path, store: RosterStore, tmp_path: Path) -> None:
    """Published specialist.toml contains no memory data (memory lives in memory.md)."""
    spec = Specialist.from_template("myspec", "backend")
    store.save(spec)
    store.append_memory("myspec", "private lesson learned in production")

    output_dir = tmp_path / "out"
    runner = CliRunner()
    runner.invoke(
        app,
        ["specialist", "publish", "myspec", "--output-dir", str(output_dir)],
    )

    toml_text = (output_dir / "specialist.toml").read_text()
    assert "private lesson" not in toml_text
    # No memory.md or stats.json should be written.
    assert not (output_dir / "memory.md").exists()
    assert not (output_dir / "stats.json").exists()
