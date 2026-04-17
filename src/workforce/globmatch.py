"""Glob utilities: pattern → regex compilation and pattern-pair overlap checks.

This module is shared by:
- `permissions.py`, which compiles each owns/excludes pattern to a regex and
  matches paths against it at write time.
- `manager.py`, which checks at plan time that two parallel tasks' lanes
  don't overlap. In a workspace dir that starts sparse, file-set intersection
  isn't enough — we need pattern-set overlap so the Manager can reject a plan
  before any agent runs and clobbers another's output.

Supported wildcards (POSIX-style, gitignore-flavored):
- ``*`` matches anything except ``/``
- ``?`` matches a single character except ``/``
- ``**`` (as a path component) matches zero or more path components
- ``[chars]`` character class (literal characters only — no negation, no ranges)

`**` is only recognized when it stands alone as a full path component;
``foo**`` is treated as the literal segment ``foo`` followed by a single ``*``
(so ``foo**`` matches the same set as ``foo*``).
"""

from __future__ import annotations

import re
from functools import lru_cache

# A component-level token is one of:
#   - "*" or "?" (literal wildcard chars)
#   - a single literal character (str of length 1)
#   - ("class", frozenset[str]) for char classes
_Token = str | tuple[str, frozenset[str]]

# ----- pattern → regex ------------------------------------------------------


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a path-glob to an anchored regex.

    Why this exists: Python 3.13 added ``**`` support to ``fnmatch.translate``
    and ``PurePath.match``, but workforce supports 3.11+. Rolling our own keeps
    the dependency footprint flat and behavior consistent across versions.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # `**/` — zero or more components.
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:[^/]+/)*")
                    i += 3
                    continue
                # `**` at end of pattern — anything, including across slashes.
                out.append(".*")
                i += 2
                continue
            # Single `*` — anything within one component.
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        if c == "[":
            # Character class — preserve up to the closing `]`.
            j = i + 1
            while j < n and pattern[j] != "]":
                j += 1
            out.append(pattern[i : j + 1])
            i = j + 1
            continue
        out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


# ----- pattern-pair overlap -------------------------------------------------

# A "segment" is one path component. We tokenize each pattern at slashes;
# `**` becomes a special DOUBLE_STAR sentinel; everything else stays as a
# component string for char-level matching.
_DOUBLE_STAR = "**"


def globs_overlap(pattern_a: str, pattern_b: str) -> bool:
    """True iff there exists at least one path string matching both patterns.

    Catches overlaps the file-set check (`{glob a} ∩ {glob b}` against the
    filesystem) misses when the directory is sparse or empty.
    """
    a = pattern_a.split("/")
    b = pattern_b.split("/")
    return _segs_overlap(tuple(a), tuple(b))


@lru_cache(maxsize=4096)
def _segs_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """Recursive NFA-style check: do path-segment sequences *a* and *b* overlap?"""
    if not a and not b:
        return True
    if not a:
        return all(s == _DOUBLE_STAR for s in b)
    if not b:
        return all(s == _DOUBLE_STAR for s in a)

    sa, sb = a[0], b[0]

    if sa == _DOUBLE_STAR:
        # `**` can match zero components (drop it from a, keep b)
        # or one+ components (consume one segment from b, keep `**` at front of a).
        if _segs_overlap(a[1:], b):
            return True
        return _segs_overlap(a, b[1:])
    if sb == _DOUBLE_STAR:
        # Symmetric.
        if _segs_overlap(a, b[1:]):
            return True
        return _segs_overlap(a[1:], b)

    # Both single-component segments — must overlap here AND the tails must overlap.
    if not _component_overlap(sa, sb):
        return False
    return _segs_overlap(a[1:], b[1:])


# ----- single-component overlap ---------------------------------------------

# Within one path component (no /), tokenize into:
# - "*" (matches any chars, including empty)
# - "?" (matches exactly one char)
# - ("class", frozenset[str])  — char class
# - any single literal character


def _tokenize_component(part: str) -> tuple[_Token, ...]:
    """Tokenize a single path component (no slashes) into a token sequence."""
    out: list[_Token] = []
    i = 0
    n = len(part)
    while i < n:
        c = part[i]
        if c in ("*", "?"):
            out.append(c)
            i += 1
            continue
        if c == "[":
            # Find closing `]`. If none, treat the `[` as a literal.
            try:
                j = part.index("]", i + 1)
            except ValueError:
                out.append(c)
                i += 1
                continue
            chars = frozenset(part[i + 1 : j])
            out.append(("class", chars))
            i = j + 1
            continue
        out.append(c)
        i += 1
    return tuple(out)


def _component_overlap(a: str, b: str) -> bool:
    """True iff two single-component patterns can match the same string."""
    return _tokens_overlap(_tokenize_component(a), _tokenize_component(b))


@lru_cache(maxsize=4096)
def _tokens_overlap(a: tuple[_Token, ...], b: tuple[_Token, ...]) -> bool:
    """Recursive NFA-style check: do token sequences *a* and *b* overlap?"""
    if not a and not b:
        return True
    if not a:
        return all(t == "*" for t in b)
    if not b:
        return all(t == "*" for t in a)

    ta, tb = a[0], b[0]
    if ta == "*":
        # `*` can absorb zero or more chars: drop it (zero) or absorb tb (one+).
        if _tokens_overlap(a[1:], b):
            return True
        return _tokens_overlap(a, b[1:])
    if tb == "*":
        if _tokens_overlap(a, b[1:]):
            return True
        return _tokens_overlap(a[1:], b)

    # Both non-`*` — must match at this position.
    if not _char_overlap(ta, tb):
        return False
    return _tokens_overlap(a[1:], b[1:])


def _char_overlap(t1: _Token, t2: _Token) -> bool:
    """Do two non-`*` tokens share at least one matching character?"""
    if t1 == "?" or t2 == "?":
        return True
    s1 = t1[1] if isinstance(t1, tuple) else frozenset({t1})
    s2 = t2[1] if isinstance(t2, tuple) else frozenset({t2})
    return bool(s1 & s2)
