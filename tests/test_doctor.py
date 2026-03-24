from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from workforce import doctor


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WORKFORCE_HOME", str(tmp_path))
    return tmp_path


def test_check_python_passes_on_current_interpreter() -> None:
    c = doctor.check_python()
    assert c.status is doctor.Status.OK
    assert f"{sys.version_info.major}.{sys.version_info.minor}" in c.detail


def test_check_python_fails_when_too_old() -> None:
    with patch.object(doctor, "MIN_PYTHON", (99, 0)):
        c = doctor.check_python()
    assert c.status is doctor.Status.FAIL


def test_check_sdk_reports_status() -> None:
    c = doctor.check_sdk()
    # We don't pin the result — depends on whether SDK is installed in this env.
    assert c.status in (doctor.Status.OK, doctor.Status.FAIL)
    assert c.name == "claude-agent-sdk"


def test_check_git_passes() -> None:
    c = doctor.check_git()
    assert c.status is doctor.Status.OK


def test_check_auth_warns_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = doctor.check_auth()
    assert c.status is doctor.Status.WARN


def test_check_auth_ok_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    c = doctor.check_auth()
    assert c.status is doctor.Status.OK


def test_check_home_creates_layout(isolated_home: Path) -> None:
    c = doctor.check_home()
    assert c.status is doctor.Status.OK
    assert (isolated_home / "roster").is_dir()
    assert (isolated_home / "projects").is_dir()


def test_worst_picks_fail() -> None:
    checks = [
        doctor.Check("a", doctor.Status.OK, ""),
        doctor.Check("b", doctor.Status.WARN, ""),
        doctor.Check("c", doctor.Status.FAIL, ""),
    ]
    assert doctor.worst(checks) is doctor.Status.FAIL


def test_worst_picks_warn() -> None:
    checks = [
        doctor.Check("a", doctor.Status.OK, ""),
        doctor.Check("b", doctor.Status.WARN, ""),
    ]
    assert doctor.worst(checks) is doctor.Status.WARN


def test_worst_all_ok() -> None:
    checks = [doctor.Check("a", doctor.Status.OK, "")]
    assert doctor.worst(checks) is doctor.Status.OK
