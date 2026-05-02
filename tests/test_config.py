"""Tests for workforce.config: load_global_config()."""

from __future__ import annotations

from pathlib import Path

import pytest

from workforce.config import GlobalConfig, load_global_config


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
    return tmp_path


def test_load_global_config_missing_file(isolated_home: Path) -> None:
    """Returns a default GlobalConfig when config.toml is absent."""
    cfg = load_global_config()
    assert isinstance(cfg, GlobalConfig)
    assert cfg.default_model is None
    assert cfg.max_turns is None
    assert cfg.max_cost is None


def test_load_global_config_parses_values(isolated_home: Path) -> None:
    """Parses all three known keys from a well-formed config.toml."""
    config_file = isolated_home / "config.toml"
    config_file.write_text(
        'default_model = "claude-sonnet-4-5"\n'
        "max_turns = 80\n"
        "max_cost = 12.5\n"
    )
    cfg = load_global_config()
    assert cfg.default_model == "claude-sonnet-4-5"
    assert cfg.max_turns == 80
    assert cfg.max_cost == 12.5


def test_load_global_config_partial_keys(isolated_home: Path) -> None:
    """Omitted keys remain None even when other keys are present."""
    config_file = isolated_home / "config.toml"
    config_file.write_text("max_turns = 30\n")
    cfg = load_global_config()
    assert cfg.max_turns == 30
    assert cfg.default_model is None
    assert cfg.max_cost is None


def test_load_global_config_bad_toml_warns(isolated_home: Path) -> None:
    """Malformed TOML emits a warning and returns default GlobalConfig."""
    config_file = isolated_home / "config.toml"
    config_file.write_text("this is not valid toml !!!\n")
    with pytest.warns(UserWarning, match="Could not parse config.toml"):
        cfg = load_global_config()
    assert isinstance(cfg, GlobalConfig)
    assert cfg.max_turns is None


def test_load_global_config_empty_file(isolated_home: Path) -> None:
    """An empty config.toml is valid and returns all-None defaults."""
    config_file = isolated_home / "config.toml"
    config_file.write_text("")
    cfg = load_global_config()
    assert isinstance(cfg, GlobalConfig)
    assert cfg.max_turns is None
    assert cfg.max_cost is None
    assert cfg.default_model is None
