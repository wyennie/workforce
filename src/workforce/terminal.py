"""Open an OS-level terminal window running a given command.

This is the "spawn a real terminal window" primitive used by `workforce
dispatch --window` and the Manager chat session: when the Manager dispatches
a worker, we want a fresh terminal window to pop up streaming that worker's
output, so the user can watch the worker without losing their place in the
Manager conversation.

Cross-platform contract:
- macOS: tells Terminal.app via `osascript` to open a new window.
- Windows: uses `start` to launch a new console.
- Linux desktop session: tries common terminal emulators in priority order.
- Headless server (no DISPLAY/WAYLAND_DISPLAY, no Terminal.app, no console):
  returns False without spawning anything. Caller falls back to printing the
  command for the user to copy-paste.

Detection happens once per call; we don't cache (terminals come and go on
remote sessions, and the cost of running `shutil.which` for ~10 names is
negligible compared to spawning a window).
"""

from __future__ import annotations

import os
import platform
import shlex
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

# A factory takes (title, command_argv, cwd) and returns the argv we should
# Popen to spawn a new terminal window running command_argv with that title.
_Factory = Callable[[str, list[str], Path | None], list[str]]


# ----- Linux factories ------------------------------------------------------


def _ghostty(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    # Ghostty uses `-e <command>` to run a command in a new window. Title
    # isn't exposed as a CLI flag; the window picks its own.
    out = ["ghostty"]
    if cwd is not None:
        out += [f"--working-directory={cwd}"]
    return out + ["-e"] + cmd


def _ptyxis(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    out = ["ptyxis", "--new-window", "-T", title]
    if cwd is not None:
        out += ["-d", str(cwd)]
    return out + ["--"] + cmd


def _kitty(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    out = ["kitty", "-T", title]
    if cwd is not None:
        out += ["--directory", str(cwd)]
    return out + cmd


def _alacritty(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    out = ["alacritty", "--title", title]
    if cwd is not None:
        out += ["--working-directory", str(cwd)]
    return out + ["-e"] + cmd


def _wezterm(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    out = ["wezterm", "start"]
    if cwd is not None:
        out += ["--cwd", str(cwd)]
    return out + ["--"] + cmd


def _foot(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    out = ["foot", "-T", title]
    if cwd is not None:
        out += ["--working-directory", str(cwd)]
    return out + cmd


def _kgx(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    # GNOME Console; -e takes a single command string.
    cmd_str = " ".join(shlex.quote(a) for a in cmd)
    out = ["kgx", "-T", title]
    if cwd is not None:
        out += ["--working-directory", str(cwd)]
    return out + ["-e", cmd_str]


def _gnome_terminal(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    out = ["gnome-terminal", f"--title={title}"]
    if cwd is not None:
        out += [f"--working-directory={cwd}"]
    return out + ["--"] + cmd


def _konsole(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    out = ["konsole", "-p", f"tabtitle={title}"]
    if cwd is not None:
        out += ["--workdir", str(cwd)]
    return out + ["-e"] + cmd


def _xfce4_terminal(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    cmd_str = " ".join(shlex.quote(a) for a in cmd)
    out = ["xfce4-terminal", f"--title={title}"]
    if cwd is not None:
        out += [f"--working-directory={cwd}"]
    return out + [f"--command={cmd_str}"]


def _tilix(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    cmd_str = " ".join(shlex.quote(a) for a in cmd)
    out = ["tilix", "--title", title]
    if cwd is not None:
        out += ["--working-directory", str(cwd)]
    return out + ["-e", cmd_str]


def _terminator(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    cmd_str = " ".join(shlex.quote(a) for a in cmd)
    out = ["terminator", "-T", title]
    if cwd is not None:
        out += ["--working-directory", str(cwd)]
    return out + ["-x", cmd_str]


def _urxvt(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    out = ["urxvt", "-title", title]
    if cwd is not None:
        out += ["-cd", str(cwd)]
    return out + ["-e"] + cmd


def _xterm(title: str, cmd: list[str], cwd: Path | None) -> list[str]:
    # xterm has no built-in cwd flag — wrap in `bash -c "cd ...; exec ..."`.
    if cwd is not None:
        cmd_str = (
            f"cd {shlex.quote(str(cwd))} && "
            + " ".join(shlex.quote(a) for a in cmd)
        )
        return ["xterm", "-T", title, "-e", "bash", "-c", cmd_str]
    return ["xterm", "-T", title, "-e"] + cmd


# Order matters — we try modern/desktop terminals first, fall back to xterm.
# x-terminal-emulator (Debian's symlink) is xterm-compatible by convention.
_LINUX_REGISTRY: list[tuple[str, _Factory]] = [
    ("ghostty", _ghostty),
    ("ptyxis", _ptyxis),
    ("kitty", _kitty),
    ("alacritty", _alacritty),
    ("wezterm", _wezterm),
    ("foot", _foot),
    ("kgx", _kgx),
    ("gnome-terminal", _gnome_terminal),
    ("konsole", _konsole),
    ("xfce4-terminal", _xfce4_terminal),
    ("tilix", _tilix),
    ("terminator", _terminator),
    ("urxvt", _urxvt),
    ("xterm", _xterm),
    ("x-terminal-emulator", _xterm),
]


# ----- parent-terminal detection -------------------------------------------


def _detect_parent_terminal() -> str | None:
    """Best-effort guess at which terminal emulator the user is running in.

    Returns a registry key (e.g., ``"ghostty"``, ``"kitty"``) so the spawner
    prefers that one for the popup, matching what the user already has open.

    Strategy, in priority order:

    1. Terminal-specific env vars set by the emulator itself (most reliable).
    2. ``$TERM_PROGRAM`` (some terminals set this).
    3. ``$VTE_VERSION`` narrows to libvte-based terminals; we then pick the
       first installed one.
    4. Walk ``/proc`` upward from our parent looking for a known terminal
       name (catches cases where env detection fails — tmux nesting, etc.).

    Returns None if no terminal can be identified — caller falls back to the
    registry's default priority order.
    """
    env = os.environ

    # 1. Terminal-specific env vars (each terminal sets its own).
    if env.get("KITTY_PID") or env.get("KITTY_WINDOW_ID"):
        return "kitty"
    if env.get("WEZTERM_PANE") or env.get("WEZTERM_EXECUTABLE"):
        return "wezterm"
    if env.get("ALACRITTY_LOG") or env.get("ALACRITTY_SOCKET"):
        return "alacritty"
    if env.get("KONSOLE_VERSION") or env.get("KONSOLE_DBUS_SESSION"):
        return "konsole"
    if env.get("TERMINATOR_UUID"):
        return "terminator"
    if env.get("TILIX_ID"):
        return "tilix"
    if env.get("GHOSTTY_RESOURCES_DIR") or env.get("GHOSTTY_BIN_DIR"):
        return "ghostty"
    if env.get("FOOT_VERSION") or env.get("TERM") == "foot":
        return "foot"

    # 2. $TERM_PROGRAM is set by some terminals.
    term_program = env.get("TERM_PROGRAM", "").lower()
    direct_map = {
        "ghostty": "ghostty",
        "kitty": "kitty",
        "wezterm": "wezterm",
        "alacritty": "alacritty",
        "vscode": None,           # VSCode integrated — fall through
        "apple_terminal": None,   # macOS path handles this
        "iterm.app": None,
    }
    if term_program in direct_map:
        mapped = direct_map[term_program]
        if mapped is not None:
            return mapped

    # 3. $VTE_VERSION narrows to libvte family; pick the first installed one.
    if env.get("VTE_VERSION"):
        for vte_name in ("ptyxis", "kgx", "gnome-terminal", "xfce4-terminal", "tilix", "terminator"):
            if shutil.which(vte_name) is not None:
                return vte_name

    # 4. Process tree walk (Linux only).
    if platform.system() == "Linux":
        return _ancestor_terminal_from_proc()
    return None


_KNOWN_TERMINAL_COMMS: dict[str, str] = {
    # Map /proc/*/comm names to registry keys. Some binaries differ from the
    # CLI name (gnome-terminal-server vs gnome-terminal).
    "ghostty": "ghostty",
    "ptyxis": "ptyxis",
    "kitty": "kitty",
    "alacritty": "alacritty",
    "wezterm": "wezterm",
    "wezterm-gui": "wezterm",
    "foot": "foot",
    "kgx": "kgx",
    "gnome-terminal-": "gnome-terminal",  # comm is truncated on Linux (16 chars)
    "konsole": "konsole",
    "xfce4-terminal": "xfce4-terminal",
    "tilix": "tilix",
    "terminator": "terminator",
    "urxvt": "urxvt",
    "rxvt-unicode": "urxvt",
    "xterm": "xterm",
    "x-terminal-emul": "x-terminal-emulator",  # truncated
}


def _ancestor_terminal_from_proc() -> str | None:
    """Walk /proc up from our parent, returning the first known terminal we
    find. Linux-only; safe to call elsewhere (returns None)."""
    try:
        pid = os.getppid()
    except OSError:
        return None
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
        except (FileNotFoundError, PermissionError, OSError):
            return None
        if comm in _KNOWN_TERMINAL_COMMS:
            return _KNOWN_TERMINAL_COMMS[comm]
        # Read parent pid from /proc/<pid>/status
        try:
            with open(f"/proc/{pid}/status") as f:
                ppid = None
                for line in f:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                        break
            if ppid is None or ppid <= 0:
                return None
            pid = ppid
        except (FileNotFoundError, PermissionError, OSError, ValueError):
            return None
    return None


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _spawn_linux(title: str, cmd: list[str], cwd: Path | None) -> bool:
    if not _has_display():
        return False

    # Build the candidate list, with priority:
    #   1. $TERMINAL env var (explicit user preference).
    #   2. Detected parent terminal — match what the user is already using.
    #   3. The default registry order.
    #
    # `seen` keeps each name to one slot so we don't try the same terminal
    # twice if it's both detected AND in the registry.
    candidates: list[tuple[str, _Factory]] = []
    seen: set[str] = set()

    by_name = dict(_LINUX_REGISTRY)

    user_pick = os.environ.get("TERMINAL")
    if user_pick:
        key = os.path.basename(user_pick)
        if key in by_name and key not in seen:
            candidates.append((key, by_name[key]))
            seen.add(key)

    detected = _detect_parent_terminal()
    if detected and detected in by_name and detected not in seen:
        candidates.append((detected, by_name[detected]))
        seen.add(detected)

    for name, factory in _LINUX_REGISTRY:
        if name in seen:
            continue
        candidates.append((name, factory))
        seen.add(name)

    for name, factory in candidates:
        if shutil.which(name) is None:
            continue
        argv = factory(title, cmd, cwd)
        try:
            subprocess.Popen(  # noqa: S603 — argv list, no shell
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except (OSError, FileNotFoundError):
            continue
    return False


# ----- macOS ----------------------------------------------------------------


def _ansi_c_quote(s: str) -> str:
    """Shell-quote *s* using ANSI-C ``$'...'`` syntax.

    ``shlex.quote`` handles embedded single-quotes via the ``'"'"'`` trick,
    which introduces double-quote characters.  When the resulting shell
    command is then embedded in an AppleScript double-quoted string literal,
    we must escape every ``"`` as ``\"``.  That second escaping breaks the
    ``'"'"'`` structure: the shell parser sees the backtick (or ``$()``)
    that was supposed to be inside a single-quoted region as unquoted text,
    enabling command injection.

    ``$'...'`` syntax sidesteps the conflict entirely: only ``\\`` and ``\\'``
    need escaping, and neither is a double-quote.  Backticks and ``$()``
    are inert inside ``$'...'`` regardless of surrounding context, and the
    syntax is supported by both ``bash`` and ``zsh`` (the only login shells
    used on modern macOS).
    """
    escaped = s.replace("\\", "\\\\").replace("'", "\\'")
    return f"$'{escaped}'"


def _spawn_macos(title: str, cmd: list[str], cwd: Path | None) -> bool:
    # AppleScript: open a new Terminal window and run the command.
    # Use ANSI-C $'...' quoting (not shlex.quote) so the result contains no
    # double-quote characters that would be mangled by the AppleScript string
    # escaping below.
    cmd_str = " ".join(_ansi_c_quote(a) for a in cmd)
    if cwd is not None:
        cmd_str = f"cd {_ansi_c_quote(str(cwd))} && {cmd_str}"
    # Escape for AppleScript double-quoted string: \ → \\, " → \"
    escaped = cmd_str.replace("\\", "\\\\").replace('"', '\\"')
    title_escaped = title.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Terminal"\n'
        f'    do script "{escaped}"\n'
        f'    set custom title of front window to "{title_escaped}"\n'
        "    activate\n"
        "end tell"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


# ----- Windows --------------------------------------------------------------


def _spawn_windows(title: str, cmd: list[str], cwd: Path | None) -> bool:
    # `start "title" cmd /k <command>` opens a new console; /k keeps it open.
    cmd_str = " ".join(shlex.quote(a) for a in cmd)
    full = f'start "{title}" cmd /k "{cmd_str}"'
    try:
        subprocess.Popen(  # noqa: S602 — using shell=True is required for `start`
            full,
            shell=True,
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


# ----- public API -----------------------------------------------------------


def open_terminal_window(
    title: str,
    command: list[str],
    *,
    cwd: Path | None = None,
) -> bool:
    """Pop up an OS-level terminal window running `command`.

    Returns True if a window was spawned; False if no terminal is available
    (headless server, no $DISPLAY, no installed emulator). Callers should
    fall back to printing the command when False.

    `command` is an argv list; the first element should be the executable.
    `cwd` is set as the new shell's working directory.
    """
    system = platform.system()
    if system == "Darwin":
        return _spawn_macos(title, command, cwd)
    if system == "Windows":
        return _spawn_windows(title, command, cwd)
    if system == "Linux":
        return _spawn_linux(title, command, cwd)
    return False
