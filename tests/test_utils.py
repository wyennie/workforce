"""Unit tests for workforce.utils._FENCE_RE and related helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from workforce.utils import _FENCE_RE
from workforce.utils import _atomic_write


class TestFenceRE:
    """Tests for _FENCE_RE — the regex that extracts fenced code blocks."""

    def test_standard_multiline_block(self) -> None:
        """Standard multi-line fenced block should match and capture the body."""
        text = "```json\n{\"key\": \"value\"}\n```"
        matches = _FENCE_RE.findall(text)
        assert matches == ['{"key": "value"}']

    def test_block_with_no_newline_after_fence(self) -> None:
        """Block where content starts on the same line as the opening fence."""
        text = "```json{\"key\": \"value\"}\n```"
        matches = _FENCE_RE.findall(text)
        assert matches == ['{"key": "value"}']

    def test_block_with_no_language_tag(self) -> None:
        """Plain ``` without a language tag should also match."""
        text = "```\nhello world\n```"
        matches = _FENCE_RE.findall(text)
        assert matches == ["hello world"]

    def test_plain_text_with_no_fence_returns_no_match(self) -> None:
        """Text without any fenced code blocks produces an empty match list."""
        text = "This is just plain text with no fences."
        matches = _FENCE_RE.findall(text)
        assert matches == []

    def test_multiple_fenced_blocks(self) -> None:
        """Multiple fenced blocks all get captured."""
        text = (
            "before\n"
            "```json\n{\"a\": 1}\n```\n"
            "middle\n"
            "```json\n{\"b\": 2}\n```\n"
            "after"
        )
        matches = _FENCE_RE.findall(text)
        assert len(matches) == 2
        assert '{"a": 1}' in matches
        assert '{"b": 2}' in matches

    def test_empty_block_matches(self) -> None:
        """An empty fenced block (no content) should match and capture ''."""
        text = "```json\n```"
        matches = _FENCE_RE.findall(text)
        # The \n? makes this match with empty content
        assert len(matches) == 1

    def test_multiline_content_captured(self) -> None:
        """Multi-line content inside a fence is captured in full (re.DOTALL)."""
        text = "```json\n{\n  \"key\": \"value\",\n  \"num\": 42\n}\n```"
        matches = _FENCE_RE.findall(text)
        assert len(matches) == 1
        assert '"key": "value"' in matches[0]
        assert '"num": 42' in matches[0]


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
