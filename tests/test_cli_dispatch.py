"""Unit tests for cli/dispatch.py helpers: _dispatch_detached, _confirm_decomposition,
_read_ticket, and _onboard_specialist."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from workforce.cli.dispatch import (
    ConfirmDecision,
    _confirm_decomposition,
    _dispatch_detached,
    _onboard_specialist,
    _read_ticket,
)
from workforce import project as project_mod
from workforce.manager import Decomposition, DecompositionKind, Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture_popen() -> tuple[list[list[str]], object]:
    """Return (captured_argvs, popen_side_effect)."""
    captured: list[list[str]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> MagicMock:
        captured.append(list(argv))
        proc = MagicMock()
        proc.pid = 99
        return proc

    return captured, fake_popen


def _minimal_detached(
    *,
    auto_merge: bool = False,
    merge_into: str | None = None,
    auto_staff: bool = True,
    panels: bool = False,
    yes: bool = False,
    open_window: bool = False,
    branch: str | None = None,
    ci: bool = False,
    output_file: str | None = None,
    require_review: bool = False,
) -> list[str]:
    """Call _dispatch_detached with benign defaults; return the captured subprocess argv."""
    captured, fake_popen = _capture_popen()
    with patch("workforce.cli.dispatch.subprocess.Popen", side_effect=fake_popen):
        with patch("workforce.terminal.open_terminal_window", return_value=False):
            _dispatch_detached(
                project_ref="myproj",
                ticket="do a thing",
                specialist="alice",
                mission_id_override="m-test-fixed",
                max_turns=10,
                max_cost=1.0,
                max_wall=300.0,
                review=False,
                max_revisions=3,
                open_window=open_window,
                branch=branch,
                auto_merge=auto_merge,
                merge_into=merge_into,
                auto_staff=auto_staff,
                panels=panels,
                yes=yes,
                ci=ci,
                output_file=output_file,
                require_review=require_review,
            )
    assert len(captured) == 1, "expected exactly one Popen call"
    return captured[0]


# ---------------------------------------------------------------------------
# _dispatch_detached: argv flag tests
# ---------------------------------------------------------------------------


def test_detached_auto_merge_included_when_true() -> None:
    argv = _minimal_detached(auto_merge=True)
    assert "--auto-merge" in argv


def test_detached_auto_merge_absent_when_false() -> None:
    argv = _minimal_detached(auto_merge=False)
    assert "--auto-merge" not in argv


def test_detached_merge_into_included_with_value() -> None:
    argv = _minimal_detached(merge_into="main")
    assert "--merge-into" in argv
    idx = argv.index("--merge-into")
    assert argv[idx + 1] == "main"


def test_detached_merge_into_absent_when_none() -> None:
    argv = _minimal_detached(merge_into=None)
    assert "--merge-into" not in argv


def test_detached_no_auto_staff_included_when_disabled() -> None:
    argv = _minimal_detached(auto_staff=False)
    assert "--no-auto-staff" in argv


def test_detached_no_auto_staff_absent_when_enabled() -> None:
    argv = _minimal_detached(auto_staff=True)
    assert "--no-auto-staff" not in argv


def test_detached_panels_included_when_true() -> None:
    argv = _minimal_detached(panels=True)
    assert "--panels" in argv


def test_detached_panels_absent_when_false() -> None:
    argv = _minimal_detached(panels=False)
    assert "--panels" not in argv


def test_detached_yes_included_when_true() -> None:
    argv = _minimal_detached(yes=True)
    assert "--yes" in argv


def test_detached_yes_absent_when_false() -> None:
    argv = _minimal_detached(yes=False)
    assert "--yes" not in argv


def test_detached_all_optional_flags_present_together() -> None:
    """All optional flags appear when enabled simultaneously."""
    argv = _minimal_detached(
        auto_merge=True,
        merge_into="feature",
        auto_staff=False,
        panels=True,
        yes=True,
    )
    assert "--auto-merge" in argv
    assert "--merge-into" in argv
    assert "feature" in argv
    assert "--no-auto-staff" in argv
    assert "--panels" in argv
    assert "--yes" in argv


def test_detached_child_never_gets_window_or_background() -> None:
    """The child subprocess never receives --window or --background (would loop forever)."""
    # background mode (open_window=False)
    argv = _minimal_detached(open_window=False)
    assert "--window" not in argv
    assert "--background" not in argv

    # window mode (open_window=True) — terminal spawner is mocked inside _minimal_detached
    argv_w = _minimal_detached(open_window=True)
    assert "--window" not in argv_w
    assert "--background" not in argv_w


def test_detached_uses_provided_mission_id_override() -> None:
    argv = _minimal_detached()
    assert "--mission-id" in argv
    idx = argv.index("--mission-id")
    assert argv[idx + 1] == "m-test-fixed"


def test_detached_generates_mission_id_when_override_is_none() -> None:
    captured, fake_popen = _capture_popen()
    with patch("workforce.cli.dispatch.subprocess.Popen", side_effect=fake_popen):
        _dispatch_detached(
            project_ref="p",
            ticket="t",
            specialist="alice",
            mission_id_override=None,  # let it auto-generate
            max_turns=5,
            max_cost=1.0,
            max_wall=60.0,
            review=False,
            max_revisions=3,
            open_window=False,
        )
    argv = captured[0]
    assert "--mission-id" in argv
    mid = argv[argv.index("--mission-id") + 1]
    assert mid.startswith("m-"), f"expected mission id prefix m-, got {mid!r}"


def test_detached_specialist_always_forwarded() -> None:
    argv = _minimal_detached()
    assert "--specialist" in argv
    idx = argv.index("--specialist")
    assert argv[idx + 1] == "alice"


def test_detached_branch_forwarded_when_set() -> None:
    argv = _minimal_detached(branch="staging")
    assert "--branch" in argv
    idx = argv.index("--branch")
    assert argv[idx + 1] == "staging"


def test_detached_branch_absent_when_none() -> None:
    argv = _minimal_detached(branch=None)
    assert "--branch" not in argv


# ---------------------------------------------------------------------------
# _confirm_decomposition: proceed / cancel / discuss paths
# ---------------------------------------------------------------------------


def _make_decomp() -> Decomposition:
    return Decomposition(
        ticket="do a thing",
        kind=DecompositionKind.PARALLEL,
        rationale="it needs parallel work",
        tasks=[
            Task(id="task1", description="Write the frontend"),
            Task(id="task2", description="Write the backend"),
        ],
    )


def _rows() -> list[tuple[str, str, str]]:
    return [
        ("task1", "alice", "already_assigned"),
        ("task2", "bob", "already_assigned"),
    ]


def test_confirm_proceed_y() -> None:
    with patch("typer.prompt", return_value="y"):
        with patch("workforce.output.rule"):
            with patch("workforce.output.info"):
                with patch("workforce.output.print_table"):
                    decision = _confirm_decomposition(_make_decomp(), _rows())
    assert decision.action == "proceed"
    assert decision.feedback == ""


def test_confirm_proceed_yes_full_word() -> None:
    with patch("typer.prompt", return_value="yes"):
        with patch("workforce.output.rule"):
            with patch("workforce.output.info"):
                with patch("workforce.output.print_table"):
                    decision = _confirm_decomposition(_make_decomp(), _rows())
    assert decision.action == "proceed"


def test_confirm_cancel_n() -> None:
    with patch("typer.prompt", return_value="n"):
        with patch("workforce.output.rule"):
            with patch("workforce.output.info"):
                with patch("workforce.output.print_table"):
                    decision = _confirm_decomposition(_make_decomp(), _rows())
    assert decision.action == "cancel"


def test_confirm_cancel_no_full_word() -> None:
    with patch("typer.prompt", return_value="no"):
        with patch("workforce.output.rule"):
            with patch("workforce.output.info"):
                with patch("workforce.output.print_table"):
                    decision = _confirm_decomposition(_make_decomp(), _rows())
    assert decision.action == "cancel"


def test_confirm_discuss_returns_feedback() -> None:
    """'d' followed by non-empty feedback returns action=discuss with the typed text."""
    with patch("typer.prompt", side_effect=["d", "please split task1 into two"]):
        with patch("workforce.output.rule"):
            with patch("workforce.output.info"):
                with patch("workforce.output.print_table"):
                    decision = _confirm_decomposition(_make_decomp(), _rows())
    assert decision.action == "discuss"
    assert decision.feedback == "please split task1 into two"


def test_confirm_discuss_full_word() -> None:
    with patch("typer.prompt", side_effect=["discuss", "make it sequential"]):
        with patch("workforce.output.rule"):
            with patch("workforce.output.info"):
                with patch("workforce.output.print_table"):
                    decision = _confirm_decomposition(_make_decomp(), _rows())
    assert decision.action == "discuss"
    assert decision.feedback == "make it sequential"


def test_confirm_discuss_empty_feedback_stays_in_loop() -> None:
    """Empty feedback after 'd' does not exit — the loop continues and accepts a fresh choice."""
    # sequence: "d", "" (empty → stay), then "y" to finally proceed
    with patch("typer.prompt", side_effect=["d", "", "y"]):
        with patch("workforce.output.rule"):
            with patch("workforce.output.info"):
                with patch("workforce.output.print_table"):
                    decision = _confirm_decomposition(_make_decomp(), _rows())
    assert decision.action == "proceed"


def test_confirm_unknown_choice_retries_until_valid() -> None:
    """Unknown input triggers a warning and re-prompts."""
    with patch("typer.prompt", side_effect=["maybe", "42", "n"]):
        with patch("workforce.output.rule"):
            with patch("workforce.output.info"):
                with patch("workforce.output.print_table"):
                    with patch("workforce.output.warn"):
                        decision = _confirm_decomposition(_make_decomp(), _rows())
    assert decision.action == "cancel"


# ---------------------------------------------------------------------------
# _dispatch_detached: new CI/output-file/require-review flags
# ---------------------------------------------------------------------------


def test_detached_ci_flag_forwarded() -> None:
    argv = _minimal_detached(ci=True)
    assert "--ci" in argv


def test_detached_ci_flag_absent_when_false() -> None:
    argv = _minimal_detached(ci=False)
    assert "--ci" not in argv


def test_detached_output_file_forwarded() -> None:
    argv = _minimal_detached(output_file="/tmp/summary.json")
    assert "--output-file" in argv
    idx = argv.index("--output-file")
    assert argv[idx + 1] == "/tmp/summary.json"


def test_detached_output_file_absent_when_none() -> None:
    argv = _minimal_detached(output_file=None)
    assert "--output-file" not in argv


def test_detached_require_review_forwarded() -> None:
    argv = _minimal_detached(require_review=True)
    assert "--require-review" in argv


def test_detached_require_review_absent_when_false() -> None:
    argv = _minimal_detached(require_review=False)
    assert "--require-review" not in argv


# ---------------------------------------------------------------------------
# _read_ticket: source resolution and mutual-exclusion
# ---------------------------------------------------------------------------


def test_read_ticket_from_positional() -> None:
    """Positional ticket string is returned as-is."""
    assert _read_ticket("fix the bug", None, False) == "fix the bug"


def test_read_ticket_from_file(tmp_path: Path) -> None:
    """--file PATH reads and returns the file contents."""
    f = tmp_path / "ticket.txt"
    f.write_text("add dark mode support")
    assert _read_ticket(None, str(f), False) == "add dark mode support"


def test_read_ticket_from_stdin() -> None:
    """--stdin reads from sys.stdin."""
    fake_stdin = MagicMock()
    fake_stdin.read.return_value = "stdin ticket text"
    with patch("sys.stdin", fake_stdin):
        result = _read_ticket(None, None, True)
    assert result == "stdin ticket text"


def test_read_ticket_mutual_exclusion_dies() -> None:
    """Passing more than one source raises SystemExit."""
    with patch("workforce.output.die", side_effect=SystemExit(1)):
        with pytest.raises(SystemExit):
            _read_ticket("inline", "file.txt", False)


def test_read_ticket_empty_positional_dies() -> None:
    """Empty positional ticket dies."""
    with patch("workforce.output.die", side_effect=SystemExit(1)):
        with pytest.raises(SystemExit):
            _read_ticket("   ", None, False)


def test_read_ticket_empty_file_dies(tmp_path: Path) -> None:
    """Empty --file dies."""
    f = tmp_path / "empty.txt"
    f.write_text("   \n")
    with patch("workforce.output.die", side_effect=SystemExit(1)):
        with pytest.raises(SystemExit):
            _read_ticket(None, str(f), False)


def test_read_ticket_empty_stdin_dies() -> None:
    """Empty stdin dies."""
    fake_stdin = MagicMock()
    fake_stdin.read.return_value = "  \n"
    with patch("sys.stdin", fake_stdin):
        with patch("workforce.output.die", side_effect=SystemExit(1)):
            with pytest.raises(SystemExit):
                _read_ticket(None, None, True)


def test_read_ticket_ci_mode_no_source_dies() -> None:
    """In CI mode, falling back to $EDITOR dies instead of opening an editor."""
    with patch("workforce.output.is_ci_mode", return_value=True):
        with patch("workforce.output.die", side_effect=SystemExit(1)):
            with pytest.raises(SystemExit):
                _read_ticket(None, None, False)


# ---------------------------------------------------------------------------
# _onboard_specialist: wizard flows
# ---------------------------------------------------------------------------


def _make_project(assigned: list[str] | None = None) -> project_mod.Project:
    return project_mod.Project(
        id="abc123def456",
        name="myproj",
        repo_path="/tmp/myrepo",
        assigned_specialists=assigned or [],
    )


def test_onboard_exists_and_assigned_noop() -> None:
    """No prompts when specialist already exists and is assigned."""
    roster = MagicMock()
    roster.exists.return_value = True
    proj = _make_project(assigned=["alice"])
    proj_store = MagicMock()

    result = _onboard_specialist("alice", proj, roster, proj_store, skip_prompts=False)
    assert result is proj
    roster.save.assert_not_called()
    proj_store.save.assert_not_called()


def test_onboard_not_exists_skip_prompts_dies() -> None:
    """skip_prompts=True dies immediately when specialist doesn't exist."""
    roster = MagicMock()
    roster.exists.return_value = False
    proj = _make_project()
    proj_store = MagicMock()

    with patch("workforce.output.die", side_effect=SystemExit(1)) as mock_die:
        with pytest.raises(SystemExit):
            _onboard_specialist("alice", proj, roster, proj_store, skip_prompts=True)
    mock_die.assert_called_once()


def test_onboard_hire_from_template_interactive() -> None:
    """Interactive path: choose a template → specialist is hired and saved."""
    roster = MagicMock()
    roster.exists.return_value = False
    # After hire, specialist will be checked for assignment.
    # We make exists() return True on the second call so we don't hit
    # the assignment die path.
    proj = _make_project(assigned=["alice"])  # already assigned after hire
    proj_store = MagicMock()

    with patch("typer.prompt", return_value="backend"):
        with patch("workforce.output.success"):
            result = _onboard_specialist("alice", proj, roster, proj_store, skip_prompts=False)

    roster.save.assert_called_once()
    # assigned check: alice is in assigned list, so no proj_store.save needed
    proj_store.save.assert_not_called()


def test_onboard_hire_skip_choice_dies() -> None:
    """Choosing 'skip' at the hire prompt dies with no such specialist."""
    roster = MagicMock()
    roster.exists.return_value = False
    proj = _make_project()
    proj_store = MagicMock()

    with patch("typer.prompt", return_value="skip"):
        with patch("workforce.output.die", side_effect=SystemExit(1)) as mock_die:
            with pytest.raises(SystemExit):
                _onboard_specialist("alice", proj, roster, proj_store, skip_prompts=False)
    mock_die.assert_called_once()


def test_onboard_not_assigned_skip_prompts_dies() -> None:
    """skip_prompts=True dies when specialist exists but is not assigned."""
    roster = MagicMock()
    roster.exists.return_value = True
    proj = _make_project(assigned=[])  # not assigned
    proj_store = MagicMock()

    with patch("workforce.output.die", side_effect=SystemExit(1)) as mock_die:
        with pytest.raises(SystemExit):
            _onboard_specialist("alice", proj, roster, proj_store, skip_prompts=True)
    mock_die.assert_called_once()


def test_onboard_assign_yes_interactive() -> None:
    """Interactive: confirm assign → project updated and saved."""
    roster = MagicMock()
    roster.exists.return_value = True
    proj = _make_project(assigned=[])
    proj_store = MagicMock()

    with patch("typer.confirm", return_value=True):
        with patch("workforce.output.success"):
            result = _onboard_specialist("alice", proj, roster, proj_store, skip_prompts=False)

    assert "alice" in result.assigned_specialists
    proj_store.save.assert_called_once()


def test_onboard_assign_no_interactive_dies() -> None:
    """Interactive: decline assign → dies."""
    roster = MagicMock()
    roster.exists.return_value = True
    proj = _make_project(assigned=[])
    proj_store = MagicMock()

    with patch("typer.confirm", return_value=False):
        with patch("workforce.output.die", side_effect=SystemExit(1)) as mock_die:
            with pytest.raises(SystemExit):
                _onboard_specialist("alice", proj, roster, proj_store, skip_prompts=False)
    mock_die.assert_called_once()
