"""Unit tests for workforce.utils._FENCE_RE and related helpers."""

from __future__ import annotations

from workforce.utils import _FENCE_RE


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
