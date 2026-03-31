"""Tests for the terminal-window spawner.

We mock subprocess.Popen and shutil.which so the tests don't actually open
windows or care what's installed locally. The factory functions are pure
string-builders and easy to verify directly.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from workforce import terminal

# ----- factory string-building ----------------------------------------------


def test_ghostty_factory_basic() -> None:
    argv = terminal._ghostty("title", ["echo", "hi"], None)
    assert argv == ["ghostty", "-e", "echo", "hi"]


def test_ghostty_factory_with_cwd(tmp_path: Path) -> None:
    argv = terminal._ghostty("t", ["ls"], tmp_path)
    assert "ghostty" == argv[0]
    assert any(a.startswith("--working-directory=") for a in argv)
    assert "-e" in argv and "ls" in argv


def test_ptyxis_factory_basic() -> None:
    argv = terminal._ptyxis("title", ["echo", "hi"], None)
    assert argv == ["ptyxis", "--new-window", "-T", "title", "--", "echo", "hi"]


def test_ptyxis_factory_with_cwd(tmp_path: Path) -> None:
    argv = terminal._ptyxis("t", ["ls"], tmp_path)
    assert "-d" in argv and str(tmp_path) in argv
    assert argv[-1] == "ls"


def test_xterm_factory_wraps_cwd_in_bash(tmp_path: Path) -> None:
    """xterm has no native cwd flag — we wrap in bash -c so cd happens first."""
    argv = terminal._xterm("t", ["echo", "hi"], tmp_path)
    assert argv[0] == "xterm"
    # Last arg should be a bash -c command string containing cd
    assert "bash" in argv
    assert "-c" in argv
    cmd_str = argv[-1]
    assert "cd " in cmd_str and "echo hi" in cmd_str


def test_kgx_factory_uses_command_string() -> None:
    """GNOME Console takes a single command string after -e."""
    argv = terminal._kgx("t", ["echo", "hi there"], None)
    assert argv[0] == "kgx"
    assert "-e" in argv
    cmd_str = argv[argv.index("-e") + 1]
    assert "echo" in cmd_str
    assert "'hi there'" in cmd_str  # shlex-quoted


# ----- _spawn_linux: try in priority order, skip missing terminals -----------


@pytest.fixture
def linux_with_display(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("TERMINAL", raising=False)


def test_linux_no_display_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert terminal._spawn_linux("t", ["echo"], None) is False


def test_linux_picks_first_available(linux_with_display: None) -> None:
    """If only `xterm` is installed, that's what we use — even though earlier
    entries in the registry are preferred."""
    spawned: dict[str, Any] = {}

    def fake_which(name: str) -> str | None:
        return "/usr/bin/xterm" if name == "xterm" else None

    def fake_popen(argv: list[str], **kwargs: Any) -> Any:
        spawned["argv"] = argv
        return _FakeProcess()

    with patch.object(shutil, "which", side_effect=fake_which):
        with patch.object(subprocess, "Popen", side_effect=fake_popen):
            ok = terminal._spawn_linux("title", ["echo", "hi"], None)
    assert ok is True
    assert spawned["argv"][0] == "xterm"


def test_linux_respects_terminal_env(linux_with_display: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """$TERMINAL=alacritty should win over earlier entries."""
    monkeypatch.setenv("TERMINAL", "alacritty")
    spawned: dict[str, Any] = {}

    def fake_which(name: str) -> str | None:
        # Both ptyxis and alacritty available; $TERMINAL should pick alacritty.
        if name in ("ptyxis", "alacritty"):
            return f"/usr/bin/{name}"
        return None

    def fake_popen(argv: list[str], **kwargs: Any) -> Any:
        spawned["argv"] = argv
        return _FakeProcess()

    with patch.object(shutil, "which", side_effect=fake_which):
        with patch.object(subprocess, "Popen", side_effect=fake_popen):
            terminal._spawn_linux("t", ["echo"], None)
    assert spawned["argv"][0] == "alacritty"


def test_linux_no_terminals_installed(linux_with_display: None) -> None:
    with patch.object(shutil, "which", return_value=None):
        ok = terminal._spawn_linux("t", ["echo"], None)
    assert ok is False


# ----- parent-terminal detection --------------------------------------------


def _clear_terminal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every env var our detector reads so tests start from a clean slate."""
    for name in (
        "KITTY_PID", "KITTY_WINDOW_ID",
        "WEZTERM_PANE", "WEZTERM_EXECUTABLE",
        "ALACRITTY_LOG", "ALACRITTY_SOCKET",
        "KONSOLE_VERSION", "KONSOLE_DBUS_SESSION",
        "TERMINATOR_UUID", "TILIX_ID",
        "GHOSTTY_RESOURCES_DIR", "GHOSTTY_BIN_DIR",
        "FOOT_VERSION",
        "VTE_VERSION",
        "TERM_PROGRAM", "TERM_PROGRAM_VERSION",
        "TERMINAL", "TERM",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "env_var,value,expected",
    [
        ("KITTY_PID", "12345", "kitty"),
        ("WEZTERM_PANE", "0", "wezterm"),
        ("ALACRITTY_LOG", "/tmp/al.log", "alacritty"),
        ("KONSOLE_VERSION", "240800", "konsole"),
        ("TERMINATOR_UUID", "abc", "terminator"),
        ("TILIX_ID", "uuid-123", "tilix"),
        ("GHOSTTY_BIN_DIR", "/usr/bin", "ghostty"),
        ("GHOSTTY_RESOURCES_DIR", "/usr/share/ghostty", "ghostty"),
    ],
)
def test_detect_parent_terminal_from_specific_env(
    env_var: str, value: str, expected: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv(env_var, value)
    # Patch /proc walk to return None so we test only env-var detection.
    with patch.object(terminal, "_ancestor_terminal_from_proc", return_value=None):
        assert terminal._detect_parent_terminal() == expected


def test_detect_parent_terminal_via_term_program(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    with patch.object(terminal, "_ancestor_terminal_from_proc", return_value=None):
        assert terminal._detect_parent_terminal() == "ghostty"


def test_detect_parent_terminal_vte_picks_first_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VTE_VERSION narrows to libvte-based terminals; we pick the first that's
    installed (ptyxis is preferred over older gnome-terminal)."""
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("VTE_VERSION", "7600")

    def fake_which(name: str) -> str | None:
        # ptyxis missing, kgx installed
        if name == "kgx":
            return "/usr/bin/kgx"
        return None

    with patch.object(shutil, "which", side_effect=fake_which):
        with patch.object(terminal, "_ancestor_terminal_from_proc", return_value=None):
            assert terminal._detect_parent_terminal() == "kgx"


def test_detect_parent_terminal_unknown_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_terminal_env(monkeypatch)
    with patch.object(terminal, "_ancestor_terminal_from_proc", return_value=None):
        assert terminal._detect_parent_terminal() is None


def test_spawn_linux_prefers_detected_parent(
    linux_with_display: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If we're inside ghostty AND xterm is also installed, we should spawn
    ghostty — matching what the user is using, not the first registry entry
    that happens to be on PATH."""
    _clear_terminal_env(monkeypatch)
    monkeypatch.setenv("GHOSTTY_BIN_DIR", "/usr/bin")
    spawned: dict[str, Any] = {}

    def fake_which(name: str) -> str | None:
        # Both ghostty and xterm installed.
        return f"/usr/bin/{name}" if name in ("ghostty", "xterm") else None

    def fake_popen(argv: list[str], **kwargs: Any) -> Any:
        spawned["argv"] = argv
        return _FakeProcess()

    with patch.object(shutil, "which", side_effect=fake_which):
        with patch.object(subprocess, "Popen", side_effect=fake_popen):
            with patch.object(terminal, "_ancestor_terminal_from_proc", return_value=None):
                terminal._spawn_linux("t", ["echo", "hi"], None)
    assert spawned["argv"][0] == "ghostty"


# ----- public API dispatches by platform -------------------------------------


@pytest.mark.skipif(platform.system() != "Linux", reason="Linux-specific")
def test_open_terminal_window_calls_linux_spawner() -> None:
    with patch.object(terminal, "_spawn_linux", return_value=True) as m:
        ok = terminal.open_terminal_window("t", ["echo"])
    assert ok is True
    m.assert_called_once()


def test_open_terminal_window_unknown_platform_returns_false() -> None:
    with patch.object(platform, "system", return_value="Plan9"):
        assert terminal.open_terminal_window("t", ["echo"]) is False


# ----- helpers --------------------------------------------------------------


class _FakeProcess:
    """Minimal subprocess.Popen stand-in for the spawn tests."""

    pid = 4242

    def __init__(self) -> None:
        pass


# Keep the imports happy; pyright will see the unused import otherwise.
_ = os
