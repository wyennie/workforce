from __future__ import annotations

from pathlib import Path

import pytest

from workforce.specialist import (
    ALL_DEV_TOOLS,
    DEFAULT_MODEL,
    TEMPLATES,
    RosterError,
    RosterStore,
    Specialist,
    SpecialistStats,
    common_preamble,
)


@pytest.fixture
def store(tmp_path: Path) -> RosterStore:
    return RosterStore(root=tmp_path / "roster")


# ----- model validation ------------------------------------------------------


def test_valid_name_passes() -> None:
    s = Specialist(name="aria", role="r", base_prompt="p")
    assert s.name == "aria"


@pytest.mark.parametrize(
    "bad",
    ["", "Aria", "1foo", "foo bar", "foo!", "-foo", "_foo", "x" * 33],
)
def test_invalid_name_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        Specialist(name=bad, role="r", base_prompt="p")


def test_default_model_and_tools() -> None:
    s = Specialist(name="ben", role="r", base_prompt="p")
    assert s.model == DEFAULT_MODEL
    assert s.allowed_tools == ALL_DEV_TOOLS


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValueError):
        Specialist.model_validate(
            {"name": "x", "role": "r", "base_prompt": "p", "wat": True}
        )


# ----- templates -------------------------------------------------------------


@pytest.mark.parametrize("tmpl", sorted(TEMPLATES))
def test_each_template_constructs(tmpl: str) -> None:
    s = Specialist.from_template("aria", tmpl)
    assert s.name == "aria"
    assert s.role
    assert common_preamble("aria") in s.base_prompt
    assert s.allowed_tools  # non-empty


def test_reviewer_lacks_write_and_edit() -> None:
    s = Specialist.from_template("rev", "reviewer")
    assert "Write" not in s.allowed_tools
    assert "Edit" not in s.allowed_tools


def test_template_role_override() -> None:
    s = Specialist.from_template("aria", "backend", role="custom role")
    assert s.role == "custom role"


def test_template_model_override() -> None:
    s = Specialist.from_template("aria", "backend", model="claude-haiku-4-5")
    assert s.model == "claude-haiku-4-5"


def test_unknown_template_raises() -> None:
    with pytest.raises(ValueError, match="unknown template"):
        Specialist.from_template("aria", "no-such-template")


def test_custom_specialist_includes_preamble() -> None:
    s = Specialist.custom("aria", role="custom role")
    assert common_preamble("aria") in s.base_prompt
    assert "custom role" in s.base_prompt


# ----- store CRUD ------------------------------------------------------------


def test_save_and_load_roundtrip(store: RosterStore) -> None:
    s = Specialist.from_template("aria", "backend")
    store.save(s)
    loaded = store.load("aria")
    assert loaded == s


def test_save_creates_stats_and_memory(store: RosterStore) -> None:
    s = Specialist.from_template("aria", "backend")
    store.save(s)
    assert (store.root / "aria" / "stats.json").is_file()
    assert (store.root / "aria" / "memory.md").is_file()


def test_save_refuses_to_overwrite(store: RosterStore) -> None:
    s = Specialist.from_template("aria", "backend")
    store.save(s)
    with pytest.raises(RosterError, match="already exists"):
        store.save(s)


def test_save_overwrite_preserves_stats_and_memory(store: RosterStore) -> None:
    s = Specialist.from_template("aria", "backend")
    store.save(s)
    store.append_memory("aria", "learned X")
    store.save_stats("aria", SpecialistStats(missions_completed=3))

    s2 = Specialist.from_template("aria", "frontend")
    store.save(s2, overwrite=True)
    assert store.load_memory("aria") == "learned X\n"
    assert store.load_stats("aria").missions_completed == 3


def test_delete_removes_everything(store: RosterStore) -> None:
    s = Specialist.from_template("aria", "backend")
    store.save(s)
    store.append_memory("aria", "hello")
    store.delete("aria")
    assert not store.exists("aria")
    assert not (store.root / "aria").exists()


def test_delete_unknown_raises(store: RosterStore) -> None:
    with pytest.raises(RosterError, match="no such specialist"):
        store.delete("ghost")


def test_load_unknown_raises(store: RosterStore) -> None:
    with pytest.raises(RosterError, match="no such specialist"):
        store.load("ghost")


def test_names_empty_when_no_root(store: RosterStore) -> None:
    assert store.names() == []


def test_names_sorted(store: RosterStore) -> None:
    for n in ["casey", "aria", "ben"]:
        store.save(Specialist.from_template(n, "backend"))
    assert store.names() == ["aria", "ben", "casey"]


def test_names_ignores_dirs_without_specialist_toml(store: RosterStore) -> None:
    store.root.mkdir(parents=True)
    (store.root / "aria").mkdir()  # empty dir, no specialist.toml
    store.save(Specialist.from_template("ben", "backend"))
    assert store.names() == ["ben"]


# ----- memory & stats --------------------------------------------------------


def test_append_memory_appends(store: RosterStore) -> None:
    store.save(Specialist.from_template("aria", "backend"))
    store.append_memory("aria", "first lesson")
    store.append_memory("aria", "second lesson\n")
    text = store.load_memory("aria")
    assert text == "first lesson\nsecond lesson\n"


def test_append_memory_unknown_raises(store: RosterStore) -> None:
    with pytest.raises(RosterError):
        store.append_memory("ghost", "x")


def test_stats_default_when_missing(store: RosterStore) -> None:
    store.save(Specialist.from_template("aria", "backend"))
    stats = store.load_stats("aria")
    assert stats.missions_completed == 0
    assert stats.total_cost_usd == 0.0


def test_save_stats_roundtrip(store: RosterStore) -> None:
    store.save(Specialist.from_template("aria", "backend"))
    store.save_stats(
        "aria",
        SpecialistStats(missions_completed=5, total_cost_usd=1.23),
    )
    stats = store.load_stats("aria")
    assert stats.missions_completed == 5
    assert stats.total_cost_usd == pytest.approx(1.23)


def test_save_stats_unknown_raises(store: RosterStore) -> None:
    with pytest.raises(RosterError):
        store.save_stats("ghost", SpecialistStats())
