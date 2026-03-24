from __future__ import annotations

import os
from pathlib import Path

import pytest

from workforce import paths


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
    return tmp_path


def test_home_uses_env_var(isolated_home: Path) -> None:
    assert paths.home() == isolated_home.resolve()


def test_home_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKFORCE_HOME", raising=False)
    assert paths.home() == Path("~/.workforce").expanduser().resolve()


def test_ensure_layout_creates_dirs(isolated_home: Path) -> None:
    h = paths.ensure_layout()
    assert h == isolated_home.resolve()
    assert (isolated_home / "roster").is_dir()
    assert (isolated_home / "projects").is_dir()


def test_ensure_layout_is_idempotent(isolated_home: Path) -> None:
    paths.ensure_layout()
    paths.ensure_layout()
    assert (isolated_home / "roster").is_dir()


def test_specialist_dir(isolated_home: Path) -> None:
    assert paths.specialist_dir("aria") == isolated_home.resolve() / "roster" / "aria"


def test_project_dir(isolated_home: Path) -> None:
    assert paths.project_dir("abc123") == isolated_home.resolve() / "projects" / "abc123"
