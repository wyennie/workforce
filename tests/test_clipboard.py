"""Tests for workforce.cli._clipboard.grab_clipboard_image().

We mock both backends (_try_pillow and subprocess.run) so no real clipboard
access, Pillow installation, or xclip/wl-paste binary is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from workforce.cli._clipboard import (
    _try_pillow,
    _try_subprocess,
    grab_clipboard_image,
)

# ---------------------------------------------------------------------------
# Minimal valid PNG bytes (1×1 white pixel) for use as stubs.
# Generated once so tests don't depend on Pillow being installed.
# ---------------------------------------------------------------------------

_PNG_STUB = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ---------------------------------------------------------------------------
# grab_clipboard_image — integration-level (mocking _try_pillow)
# ---------------------------------------------------------------------------


class TestGrabClipboardImage:
    def test_returns_pillow_result_first(self) -> None:
        with patch("workforce.cli._clipboard._try_pillow", return_value=(_PNG_STUB, "image/png")):
            result = grab_clipboard_image()

        assert result is not None
        raw, media_type = result
        assert media_type == "image/png"
        assert raw == _PNG_STUB

    def test_falls_back_to_subprocess_when_pillow_returns_none(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = _PNG_STUB
            return mock

        with patch("workforce.cli._clipboard._try_pillow", return_value=None):
            with patch("subprocess.run", side_effect=fake_run):
                result = grab_clipboard_image()

        assert result is not None
        assert result[1] == "image/png"

    def test_returns_none_when_all_backends_fail(self) -> None:
        with patch("workforce.cli._clipboard._try_pillow", return_value=None):
            with patch("subprocess.run", side_effect=FileNotFoundError):
                result = grab_clipboard_image()

        assert result is None


# ---------------------------------------------------------------------------
# _try_pillow — unit tests with mocked PIL
# ---------------------------------------------------------------------------


class TestTryPillow:
    def test_returns_none_when_pillow_not_installed(self) -> None:
        with patch.dict("sys.modules", {"PIL": None, "PIL.Image": None, "PIL.ImageGrab": None}):
            result = _try_pillow()
        assert result is None

    def test_returns_none_when_grabclipboard_returns_none(self) -> None:
        # Simulate an installed Pillow where the clipboard has no image.
        fake_image_mod = MagicMock()
        fake_image_mod.Image.Image = object  # grabclipboard returns None → not instance
        fake_grab = MagicMock(return_value=None)
        fake_image_mod.ImageGrab.grabclipboard = fake_grab

        with patch.dict(
            "sys.modules",
            {"PIL": fake_image_mod, "PIL.Image": fake_image_mod.Image, "PIL.ImageGrab": fake_image_mod.ImageGrab},
        ):
            result = _try_pillow()

        assert result is None

    def test_returns_none_when_grabclipboard_raises(self) -> None:
        fake_image_mod = MagicMock()
        fake_image_mod.ImageGrab.grabclipboard.side_effect = OSError("no display")

        with patch.dict(
            "sys.modules",
            {"PIL": fake_image_mod, "PIL.Image": fake_image_mod.Image, "PIL.ImageGrab": fake_image_mod.ImageGrab},
        ):
            result = _try_pillow()

        assert result is None

    def test_returns_none_when_grabbed_is_not_image(self) -> None:
        # grabclipboard() returned a list of file paths (Linux file-copy case).
        fake_image_mod = MagicMock()
        fake_image_mod.Image.Image = object  # type check will fail → not instance

        fake_image_mod.ImageGrab.grabclipboard.return_value = ["/tmp/file.png"]

        with patch.dict(
            "sys.modules",
            {"PIL": fake_image_mod, "PIL.Image": fake_image_mod.Image, "PIL.ImageGrab": fake_image_mod.ImageGrab},
        ):
            result = _try_pillow()

        assert result is None

    def test_returns_png_bytes_when_image_grabbed(self) -> None:
        # Build a real Pillow image (if available) to test the happy path.
        try:
            from PIL import Image, ImageGrab  # noqa: F401
        except ImportError:
            pytest.skip("Pillow not installed")

        import io

        from PIL import Image as _Image

        fake_img = _Image.new("RGB", (2, 2), color=(255, 0, 0))

        with patch("PIL.ImageGrab.grabclipboard", return_value=fake_img):
            result = _try_pillow()

        assert result is not None
        raw, media_type = result
        assert media_type == "image/png"
        # Verify it's valid PNG.
        loaded = _Image.open(io.BytesIO(raw))
        assert loaded.size == (2, 2)


# ---------------------------------------------------------------------------
# _try_subprocess — unit tests
# ---------------------------------------------------------------------------


class TestTrySubprocess:
    def test_returns_bytes_on_success(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = _PNG_STUB

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = _try_subprocess(["wl-paste", "--no-newline", "--type", "image/png"])

        assert result is not None
        raw, media_type = result
        assert raw == _PNG_STUB
        assert media_type == "image/png"
        mock_run.assert_called_once()

    def test_returns_none_when_command_not_found(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = _try_subprocess(["wl-paste", "--type", "image/png"])
        assert result is None

    def test_returns_none_when_command_times_out(self) -> None:
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("wl-paste", 5)):
            result = _try_subprocess(["wl-paste", "--type", "image/png"])
        assert result is None

    def test_returns_none_when_returncode_nonzero(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = b""

        with patch("subprocess.run", return_value=mock_result):
            result = _try_subprocess(["xclip", "-o"])
        assert result is None

    def test_returns_none_when_stdout_empty(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b""

        with patch("subprocess.run", return_value=mock_result):
            result = _try_subprocess(["xclip", "-o"])
        assert result is None

    def test_wl_paste_preferred_over_xclip(self) -> None:
        """Validate that wl-paste is tried before xclip in grab_clipboard_image."""
        call_order: list[str] = []

        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            call_order.append(cmd[0])
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = _PNG_STUB
            return mock

        with patch("workforce.cli._clipboard._try_pillow", return_value=None):
            with patch("subprocess.run", side_effect=fake_run):
                grab_clipboard_image()

        assert call_order[0] == "wl-paste"

    def test_xclip_used_when_wl_paste_unavailable(self) -> None:
        def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
            if cmd[0] == "wl-paste":
                raise FileNotFoundError
            mock = MagicMock()
            mock.returncode = 0
            mock.stdout = _PNG_STUB
            return mock

        with patch("workforce.cli._clipboard._try_pillow", return_value=None):
            with patch("subprocess.run", side_effect=fake_run):
                result = grab_clipboard_image()

        assert result is not None
        assert result[1] == "image/png"
