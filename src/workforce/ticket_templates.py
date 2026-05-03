"""Ticket templates for `workforce ticket new`.

Each template is a Markdown string with <!-- comment --> placeholders to guide
the user.  Keys are the type names that may be passed to `workforce ticket new`.
"""

from __future__ import annotations

TICKET_TEMPLATES: dict[str, str] = {
    "bug-fix": """\
# Bug

<!-- One-line description of the bug. E.g. "Login button does nothing on Safari 17." -->

## Steps to reproduce

1. <!-- Step 1 -->
2. <!-- Step 2 -->
3. <!-- Step 3 -->

## Expected behaviour

<!-- What should happen. -->

## Actual behaviour

<!-- What happens instead.  Include any error messages or stack traces. -->

## Likely files involved

<!-- List the files or modules most likely to need changes. -->
""",

    "feature": """\
# Feature description

<!-- What capability are we adding?  One short paragraph. -->

## Acceptance criteria

- [ ] <!-- criterion 1 -->
- [ ] <!-- criterion 2 -->
- [ ] <!-- criterion 3 -->

## Out of scope

<!-- Explicitly list what this ticket does NOT cover to avoid scope creep. -->

## Likely files involved

<!-- List the files or modules most likely to need changes. -->
""",

    "refactor": """\
# What to refactor

<!-- Which code, module, or subsystem needs to change? -->

## Why (motivation)

<!-- What pain point, tech-debt item, or architectural goal drives this? -->

## Constraints

<!-- Any backward-compatibility requirements, performance budgets, API contracts
     that must be preserved, or out-of-scope changes. -->

## Test coverage required

<!-- Describe what tests must pass (or be added) before this is done. -->
""",

    "chore": """\
# Task description

<!-- What needs to be done?  Be concrete. -->

## Done when (acceptance)

<!-- List the conditions that make this ticket complete. -->
- [ ] <!-- condition 1 -->

## Notes

<!-- Any additional context, links, or gotchas. -->
""",

    "docs": """\
# What to document

<!-- Which feature, module, or workflow needs documentation? -->

## Audience

<!-- Who will read this?  E.g. "developers integrating the webhook API",
     "end-users running the CLI for the first time". -->

## Format

<!-- README / docstring / guide / ADR / other -->

## Related files

<!-- Source files, existing docs, or issues that provide context. -->
""",
}
