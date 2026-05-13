"""Clipboard image acquisition for the manage chat session.

Tries Pillow ``ImageGrab`` first (works on macOS, Windows, and partial
Linux when ``xclip`` or ``xsel`` are available to Pillow). Falls back to
subprocess helpers: ``wl-paste`` on Wayland, ``xclip`` on X11.

Usage::

    from workforce.cli._clipboard import grab_clipboard_image

    result = grab_clipboard_image()
    if result is not None:
        raw_bytes, media_type = result
        # raw_bytes is PNG-encoded; media_type is "image/png"
"""

from __future__ import annotations

import io
import subprocess


def grab_clipboard_image() -> tuple[bytes, str] | None:
    """Grab an image from the system clipboard.

    Returns:
        ``(raw_bytes, media_type)`` where *raw_bytes* is PNG-encoded image
        data and *media_type* is ``"image/png"``; or ``None`` when the
        clipboard contains no image or no suitable backend is available.

    Errors from missing libraries or unavailable backends are swallowed
    silently. The caller should treat ``None`` as "no image available."
    """
    # --- Pillow path (macOS, Windows, some Linux with xclip) ----------------
    data = _try_pillow()
    if data is not None:
        return data

    # --- Subprocess fallback (Linux: Wayland first, then X11) ---------------
    for cmd in [
        # wl-paste: Wayland clipboard tool (wl-clipboard package)
        ["wl-paste", "--no-newline", "--type", "image/png"],
        # xclip: X11 clipboard tool
        ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
    ]:
        data = _try_subprocess(cmd)
        if data is not None:
            return data

    return None


def _try_pillow() -> tuple[bytes, str] | None:
    """Attempt to grab the clipboard image via Pillow ImageGrab.

    Returns ``(bytes, "image/png")`` on success, ``None`` otherwise.
    """
    try:
        from PIL import Image, ImageGrab
    except ImportError:
        return None

    try:
        grabbed = ImageGrab.grabclipboard()
    except Exception:
        return None

    if grabbed is None:
        return None

    # On Linux, ImageGrab.grabclipboard() may return a list of file paths
    # instead of an Image object when clipboard contains file references.
    # We only handle actual image objects.
    if not isinstance(grabbed, Image.Image):
        return None

    try:
        buf = io.BytesIO()
        grabbed.save(buf, format="PNG")
        return buf.getvalue(), "image/png"
    except Exception:
        return None


def _try_subprocess(cmd: list[str]) -> tuple[bytes, str] | None:
    """Run *cmd* and return its stdout as PNG bytes.

    Returns ``(bytes, "image/png")`` when the command exits 0 and produces
    output, ``None`` on any failure.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0 or not result.stdout:
        return None

    return result.stdout, "image/png"


__all__ = ["grab_clipboard_image"]
