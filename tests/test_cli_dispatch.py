"""Unit tests for cli/dispatch.py: _dispatch_detached argv flags and _confirm_decomposition."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from workforce.cli.dispatch import ConfirmDecision, _confirm_decomposition, _dispatch_detached
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
