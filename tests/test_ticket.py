"""Tests for workforce ticket templates and the `workforce ticket new` command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from workforce.cli import app
from workforce.ticket_templates import TICKET_TEMPLATES


# ---------------------------------------------------------------------------
# Template content tests
# ---------------------------------------------------------------------------


def test_all_template_types_present() -> None:
    """The five expected template types are all present."""
    expected = {"bug-fix", "feature", "refactor", "chore", "docs"}
    assert expected == set(TICKET_TEMPLATES.keys())


@pytest.mark.parametrize("ticket_type", list(TICKET_TEMPLATES.keys()))
def test_template_is_non_empty_string(ticket_type: str) -> None:
    """Every template is a non-empty string."""
    tmpl = TICKET_TEMPLATES[ticket_type]
    assert isinstance(tmpl, str)
    assert len(tmpl.strip()) > 0


@pytest.mark.parametrize("ticket_type", list(TICKET_TEMPLATES.keys()))
def test_template_has_markdown_heading(ticket_type: str) -> None:
    """Every template starts with a Markdown H1 heading."""
    tmpl = TICKET_TEMPLATES[ticket_type]
    assert any(line.startswith("# ") for line in tmpl.splitlines())


def test_bug_fix_template_has_required_sections() -> None:
    """bug-fix template contains all expected section headings."""
    tmpl = TICKET_TEMPLATES["bug-fix"]
    for section in ["Bug", "Steps to reproduce", "Expected behaviour", "Actual behaviour", "Likely files involved"]:
        assert section in tmpl, f"missing section: {section!r}"


def test_feature_template_has_required_sections() -> None:
    """feature template contains all expected section headings."""
    tmpl = TICKET_TEMPLATES["feature"]
    for section in ["Feature description", "Acceptance criteria", "Out of scope", "Likely files involved"]:
        assert section in tmpl, f"missing section: {section!r}"


def test_refactor_template_has_required_sections() -> None:
    """refactor template contains all expected section headings."""
    tmpl = TICKET_TEMPLATES["refactor"]
    for section in ["What to refactor", "Why", "Constraints", "Test coverage required"]:
        assert section in tmpl, f"missing section: {section!r}"


def test_chore_template_has_required_sections() -> None:
    """chore template contains all expected section headings."""
    tmpl = TICKET_TEMPLATES["chore"]
    for section in ["Task description", "Done when", "Notes"]:
        assert section in tmpl, f"missing section: {section!r}"


def test_docs_template_has_required_sections() -> None:
    """docs template contains all expected section headings."""
    tmpl = TICKET_TEMPLATES["docs"]
    for section in ["What to document", "Audience", "Format", "Related files"]:
        assert section in tmpl, f"missing section: {section!r}"


@pytest.mark.parametrize("ticket_type", list(TICKET_TEMPLATES.keys()))
def test_template_has_placeholder_comment(ticket_type: str) -> None:
    """Every template includes at least one <!-- ... --> placeholder comment."""
    tmpl = TICKET_TEMPLATES[ticket_type]
    assert "<!--" in tmpl and "-->" in tmpl


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


runner = CliRunner()


def test_ticket_list_flag() -> None:
    """workforce ticket new --list prints all available types and exits 0."""
    result = runner.invoke(app, ["ticket", "new", "--list"])
    assert result.exit_code == 0, result.output
    for t in TICKET_TEMPLATES:
        assert t in result.output


def test_ticket_unknown_type_exits_nonzero() -> None:
    """workforce ticket new <unknown> exits with non-zero code."""
    result = runner.invoke(app, ["ticket", "new", "nonexistent-type"])
    assert result.exit_code != 0


def test_ticket_new_cancelled_when_unchanged(tmp_path: Path) -> None:
    """When the file is unchanged after editing, report cancellation."""
    def noop_editor(args: list[str], **_kw: object) -> object:
        # Do nothing — file stays as the template.
        return type("R", (), {"returncode": 0})()

    with patch("subprocess.run", side_effect=noop_editor):
        result = runner.invoke(app, ["ticket", "new", "chore"])

    assert result.exit_code == 0
    assert "cancelled" in result.output.lower()


def test_ticket_new_dispatches_hint_when_confirmed(tmp_path: Path) -> None:
    """After editing, if user says 'y', the dispatch hint is printed."""
    tmpl = TICKET_TEMPLATES["feature"]
    edited_content = tmpl + "\n## Extra section added by user\n\nsome detail\n"

    def fake_editor(args: list[str], **_kw: object) -> object:
        # Overwrite the temp file with something different from the template.
        Path(args[-1]).write_text(edited_content)
        return type("R", (), {"returncode": 0})()

    with patch("subprocess.run", side_effect=fake_editor):
        # Confirm "y" when asked "Dispatch now?"
        result = runner.invoke(app, ["ticket", "new", "feature"], input="y\n")

    assert result.exit_code == 0
    assert "workforce dispatch" in result.output


def test_ticket_new_no_dispatch_when_declined(tmp_path: Path) -> None:
    """After editing, if user says 'n', we print the temp-file path instead."""
    tmpl = TICKET_TEMPLATES["bug-fix"]
    edited_content = tmpl + "\n## User-added context\n\ndetails\n"

    def fake_editor(args: list[str], **_kw: object) -> object:
        Path(args[-1]).write_text(edited_content)
        return type("R", (), {"returncode": 0})()

    with patch("subprocess.run", side_effect=fake_editor):
        result = runner.invoke(app, ["ticket", "new", "bug-fix"], input="n\n")

    assert result.exit_code == 0
    # Should NOT print the dispatch hint
    assert "workforce dispatch" not in result.output
    # Should print the temp file path
    assert "workforce-ticket-" in result.output


def test_ticket_subcommand_registered() -> None:
    """The 'ticket' group is registered and shows in the help output."""
    result = runner.invoke(app, ["ticket", "--help"])
    assert result.exit_code == 0
    assert "new" in result.output
