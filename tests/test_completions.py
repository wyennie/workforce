"""Tests for shell completion callbacks in cli/_completions.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from workforce.cli._completions import (
    complete_mission_id,
    complete_project,
    complete_specialist,
)

# Typer passes ctx and param alongside incomplete; we don't use them.
CTX = None
ARGS = None


class TestCompleteProject:
    def test_returns_matching_names(self) -> None:
        proj_a = MagicMock()
        proj_a.name = "alpha"
        proj_b = MagicMock()
        proj_b.name = "beta"

        with patch("workforce.project.ProjectStore") as MockStore:
            MockStore.return_value.list.return_value = [proj_a, proj_b]
            result = complete_project(CTX, ARGS, "al")

        assert result == ["alpha"]

    def test_returns_all_when_incomplete_empty(self) -> None:
        proj_a = MagicMock()
        proj_a.name = "alpha"
        proj_b = MagicMock()
        proj_b.name = "beta"

        with patch("workforce.project.ProjectStore") as MockStore:
            MockStore.return_value.list.return_value = [proj_a, proj_b]
            result = complete_project(CTX, ARGS, "")

        assert sorted(result) == ["alpha", "beta"]

    def test_returns_list_on_exception(self) -> None:
        with patch("workforce.project.ProjectStore", side_effect=RuntimeError("boom")):
            result = complete_project(CTX, ARGS, "any")
        assert result == []
        assert isinstance(result, list)


class TestCompleteSpecialist:
    def test_returns_matching_names(self) -> None:
        with patch("workforce.specialist.RosterStore") as MockStore:
            MockStore.return_value.names.return_value = ["backend", "frontend", "tester"]
            result = complete_specialist(CTX, ARGS, "fr")

        assert result == ["frontend"]

    def test_returns_all_when_incomplete_empty(self) -> None:
        with patch("workforce.specialist.RosterStore") as MockStore:
            MockStore.return_value.names.return_value = ["backend", "frontend"]
            result = complete_specialist(CTX, ARGS, "")

        assert sorted(result) == ["backend", "frontend"]

    def test_returns_list_on_exception(self) -> None:
        with patch("workforce.specialist.RosterStore", side_effect=OSError("no such dir")):
            result = complete_specialist(CTX, ARGS, "x")
        assert result == []
        assert isinstance(result, list)


class TestCompleteMissionId:
    def test_returns_matching_ids(self, tmp_path) -> None:
        proj = MagicMock()
        proj.id = "proj1"

        # Create fake mission directories.
        missions_root = tmp_path / "missions_proj1"
        missions_root.mkdir()
        (missions_root / "m-abc123").mkdir()
        (missions_root / "m-abc456").mkdir()
        (missions_root / "m-xyz789").mkdir()

        with patch("workforce.project.ProjectStore") as MockStore:
            store_inst = MockStore.return_value
            store_inst.list.return_value = [proj]
            store_inst.missions_dir.return_value = missions_root
            result = complete_mission_id(CTX, ARGS, "m-abc")

        assert sorted(result) == ["m-abc123", "m-abc456"]

    def test_returns_empty_when_no_missions_dir(self, tmp_path) -> None:
        proj = MagicMock()
        proj.id = "proj1"

        nonexistent = tmp_path / "does_not_exist"

        with patch("workforce.project.ProjectStore") as MockStore:
            store_inst = MockStore.return_value
            store_inst.list.return_value = [proj]
            store_inst.missions_dir.return_value = nonexistent
            result = complete_mission_id(CTX, ARGS, "")

        assert result == []

    def test_returns_list_on_exception(self) -> None:
        with patch("workforce.project.ProjectStore", side_effect=Exception("fail")):
            result = complete_mission_id(CTX, ARGS, "m-")
        assert result == []
        assert isinstance(result, list)
