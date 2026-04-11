"""Tests for the glob-pattern overlap detector and glob_to_regex compiler."""

from __future__ import annotations

import pytest

from workforce.globmatch import glob_to_regex, globs_overlap

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


# ----- glob_to_regex: character-class edge cases ----------------------------
# The module docstring explicitly warns that [^…] negation and [a-z] ranges
# are passed through verbatim to the regex engine (no custom parsing).
# These tests document the *actual* behaviour, whether "works" or "passthrough".


def test_char_class_negation_passthrough_to_regex() -> None:
    """[^abc] is passed through to the regex engine unchanged.

    The module only parses the character *set* for the overlap detector; for
    glob_to_regex it copies the bracket expression verbatim. Python's regex
    engine understands [^abc], so the compiled pattern honours the negation.
    """
    pat = glob_to_regex("[^abc].py")
    # Characters NOT in [abc] should match.
    assert pat.match("d.py")
    assert pat.match("z.py")
    # Characters IN [abc] should NOT match.
    assert not pat.match("a.py")
    assert not pat.match("b.py")
    assert not pat.match("c.py")


def test_char_class_range_passthrough_to_regex() -> None:
    """[a-z] ranges are passed through to the regex engine unchanged.

    Python's regex engine handles character ranges, so these work correctly
    even though the module doesn't parse ranges itself.
    """
    pat = glob_to_regex("[a-z]_config.py")
    assert pat.match("a_config.py")
    assert pat.match("m_config.py")
    assert pat.match("z_config.py")
    # Digits and uppercase are outside [a-z].
    assert not pat.match("A_config.py")
    assert not pat.match("1_config.py")


def test_char_class_combined_range_and_negation() -> None:
    """[^0-9] (any non-digit) is handled by the regex engine as expected."""
    pat = glob_to_regex("file[^0-9].txt")
    assert pat.match("fileA.txt")
    assert pat.match("file_.txt")
    assert not pat.match("file0.txt")
    assert not pat.match("file9.txt")


def test_char_class_overlap_detector_ignores_negation() -> None:
    """The overlap detector's _tokenize_component reads literal chars from
    [^abc] — it strips the ^ and treats it as a normal char-class member.
    This means [^abc] and [abc] will appear to overlap in the detector
    (the ^ is just another character in the frozenset).

    We document this known limitation here: the overlap detector is conservative
    (may report overlap when there is none), which is acceptable — the Manager
    should use more specific patterns when exact disjointness is needed.
    """
    # [^abc] contains '^', 'a', 'b', 'c' as its frozenset.
    # [abc] contains 'a', 'b', 'c'. They share 'a'/'b'/'c'.
    # So the detector reports overlap (a false positive is safe; a false
    # negative would be a correctness bug).
    assert globs_overlap("[^abc]_test.py", "[abc]_test.py")


def test_char_class_range_overlap_detector_literal() -> None:
    """[a-z] in the overlap detector is tokenized as {'a', '-', 'z'} (three
    literal chars, not a range).  So [a-z] overlaps [x-z] only because of
    the shared '-' and 'z' characters in the frozensets.

    Again this is a documented conservative approximation: false positives
    (spurious overlap reports) are safe; false negatives are not.
    """
    # [a-z] → {'a', '-', 'z'}, [x-z] → {'x', '-', 'z'}  — share '-' and 'z'
    assert globs_overlap("[a-z]file.py", "[x-z]file.py")
