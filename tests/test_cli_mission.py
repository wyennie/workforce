from __future__ import annotations

import datetime as dt

import pytest
import typer

from workforce.cli_mission import _parse_duration, _parse_iso_z, _summarize_tool_args, _truncate


@pytest.mark.parametrize(
    "s,expected",
    [
        ("7d", dt.timedelta(days=7)),
        ("24h", dt.timedelta(hours=24)),
        ("2w", dt.timedelta(weeks=2)),
        ("1m", dt.timedelta(days=30)),
        (" 30D ", dt.timedelta(days=30)),
    ],
)
def test_parse_duration_valid(s: str, expected: dt.timedelta) -> None:
    assert _parse_duration(s) == expected


@pytest.mark.parametrize("bad", ["", "7", "7y", "abc", "7 days", "-7d"])
def test_parse_duration_invalid_raises(bad: str) -> None:
    with pytest.raises(typer.BadParameter):
        _parse_duration(bad)


def test_parse_iso_z() -> None:
    parsed = _parse_iso_z("2026-05-02T14:12:34Z")
    assert parsed == dt.datetime(2026, 5, 2, 14, 12, 34, tzinfo=dt.UTC)


def test_truncate_short() -> None:
    assert _truncate("hello", 10) == "hello"


def test_truncate_long() -> None:
    out = _truncate("hello world this is too long", 10)
    assert len(out) == 10
    assert out.endswith("…")


def test_summarize_tool_args_picks_known_key() -> None:
    assert "file_path=" in _summarize_tool_args("Write", {"file_path": "/tmp/x", "content": "..."})
    assert "command=" in _summarize_tool_args("Bash", {"command": "ls -la"})


def test_summarize_tool_args_falls_back_to_first() -> None:
    out = _summarize_tool_args("Custom", {"some_arg": 42})
    assert "some_arg=" in out


def test_summarize_tool_args_empty() -> None:
    assert _summarize_tool_args("X", {}) == ""
