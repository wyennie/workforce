"""Tests for the glob-pattern overlap detector."""

from __future__ import annotations

import pytest

from workforce.globmatch import globs_overlap

# ----- patterns that DO overlap ---------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        # Identical
        ("foo.py", "foo.py"),
        ("app/api/handler.py", "app/api/handler.py"),
        # Subset relations
        ("app/**", "app/api/v1.py"),
        ("**/conftest.py", "tests/conftest.py"),
        ("outputs/**", "outputs/results.json"),
        ("outputs/*.json", "outputs/results.json"),
        ("outputs/*.json", "outputs/jobs.json"),
        # Same prefix, different glob extensions sharing a witness
        ("outputs/*.json", "outputs/jobs*.json"),
        ("app/**/handler.py", "app/api/handler.py"),
        ("app/**/handler.py", "app/api/v1/handler.py"),
        # Star vs star (any common literal works as witness)
        ("*.py", "test_*.py"),
        ("foo*.py", "*bar.py"),  # foobar.py is the witness
        # Both ends bounded but compatible middle
        ("a*c", "*b*"),  # abc, axbxc, etc.
        # Char classes
        ("[ab]_test.py", "a_test.py"),
        ("[ab]_test.py", "[bc]_test.py"),  # share 'b'
        # ? wildcard
        ("file?.py", "file1.py"),
        ("a?c", "abc"),
        # ** absorbing arbitrary levels
        ("**", "a/b/c.py"),
        ("a/**/d", "a/b/c/d"),
        ("a/**/d", "a/d"),  # ** matches zero
        # Both have **
        ("a/**/x", "**/x"),
        ("**/foo", "foo/**"),  # foo itself? No, foo/** needs at least one component after.
        # Actually let's check: foo/** matches "foo/x", **/foo matches "x/foo".
        # Witness: "foo/foo" matches both — yes overlap.
    ],
)
def test_overlapping_patterns(a: str, b: str) -> None:
    assert globs_overlap(a, b), f"{a!r} should overlap {b!r}"
    assert globs_overlap(b, a), f"{b!r} should overlap {a!r} (symmetric)"


# ----- patterns that DON'T overlap ------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        # Different file extensions
        ("*.py", "*.txt"),
        ("outputs/*.json", "outputs/*.txt"),
        # Different directory trees
        ("app/**", "tests/**"),
        ("docs/**", "src/**"),
        # Same prefix, distinct trailing literals
        ("outputs/results.json", "outputs/jobs.json"),
        # Different depth requirements (one literal, one needs deeper)
        ("a/b", "a/b/c"),
        ("a/b/c", "a/b"),
        # `*` doesn't cross /
        ("app/*", "app/sub/foo"),
        ("app/*.py", "app/sub/foo.py"),
        # Different character classes
        ("[ab]_test.py", "[cd]_test.py"),
        # Literal char vs char class miss
        ("a_test.py", "[bc]_test.py"),
        # Different filenames
        ("README.md", "LICENSE"),
        # Mismatched literal segments
        ("foo/bar.py", "foo/baz.py"),
    ],
)
def test_non_overlapping_patterns(a: str, b: str) -> None:
    assert not globs_overlap(a, b), f"{a!r} should NOT overlap {b!r}"
    assert not globs_overlap(b, a), f"{b!r} should NOT overlap {a!r} (symmetric)"


# ----- regression / specific edge cases -------------------------------------


def test_empty_pattern_overlaps_only_empty() -> None:
    # Edge: "" has one segment of "" — overlaps only with itself / **.
    assert globs_overlap("", "")
    assert globs_overlap("", "**")
    assert not globs_overlap("", "foo")


def test_double_star_only() -> None:
    """`**` alone matches everything — overlaps with any pattern."""
    assert globs_overlap("**", "foo")
    assert globs_overlap("**", "a/b/c.py")
    assert globs_overlap("**", "outputs/*.json")


def test_realistic_manager_output() -> None:
    """Sanity check on patterns the Manager would plausibly emit."""
    pairs_overlap = [
        ("app/api/**", "app/api/v1.py"),
        ("tests/**", "tests/test_api.py"),
        ("outputs/jobs/*.json", "outputs/jobs/listing-2026-05-03.json"),
    ]
    pairs_disjoint = [
        ("app/api/**", "app/web/**"),
        ("outputs/jobs/**", "outputs/applications/**"),
        ("src/**", "tests/**"),
    ]
    for a, b in pairs_overlap:
        assert globs_overlap(a, b), f"expected overlap: {a} vs {b}"
    for a, b in pairs_disjoint:
        assert not globs_overlap(a, b), f"expected disjoint: {a} vs {b}"
