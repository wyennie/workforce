"""Tests for workforce.utils shared helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from workforce.utils import _atomic_write


# ----- _atomic_write ---------------------------------------------------------


def test_atomic_write_creates_file(tmp_path: Path) -> None:
    """Happy path: content lands in path and no .tmp remains."""
    dest = tmp_path / "meta.json"
    _atomic_write(dest, '{"ok": true}\n')

    assert dest.read_text() == '{"ok": true}\n'
    assert not (tmp_path / "meta.json.tmp").exists()


def test_atomic_write_tmp_name_uses_full_name(tmp_path: Path) -> None:
    """Temp file is <name>.tmp, not the suffix replaced (meta.json.tmp, not meta.tmp)."""
    dest = tmp_path / "meta.json"
    # Intercept os.replace before it runs so we can inspect the tmp file name.
    seen_tmp: list[Path] = []

    import os as _os

    real_replace = _os.replace

    def spy_replace(src: str, dst: str) -> None:
        seen_tmp.append(Path(src))
        real_replace(src, dst)

    with patch("workforce.utils.os.replace", side_effect=spy_replace):
        _atomic_write(dest, "x")

    assert len(seen_tmp) == 1
    assert seen_tmp[0].name == "meta.json.tmp"


def test_atomic_write_removes_tmp_on_replace_failure(tmp_path: Path) -> None:
    """If os.replace raises OSError, the .tmp file is deleted before re-raising."""
    dest = tmp_path / "meta.json"
    tmp = tmp_path / "meta.json.tmp"

    def failing_replace(src: str, dst: str) -> None:
        raise OSError("simulated replace failure")

    with patch("workforce.utils.os.replace", side_effect=failing_replace):
        with pytest.raises(OSError, match="simulated"):
            _atomic_write(dest, "content")

    # The destination was not written.
    assert not dest.exists()
    # The temp file was cleaned up.
    assert not tmp.exists()
