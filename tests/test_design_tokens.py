# tests/test_design_tokens.py
"""Every design token a stylesheet references must exist.

CSS is forgiving in the worst possible way: `color: var(--color-text-muted)` where no
`--color-text-muted` is defined is not an error, it is an **invalid declaration**, and the browser
drops the whole line. The element silently inherits, the page still renders, and nothing anywhere
says the rule did not apply.

Measured on 2026-09-08, five stylesheets referenced tokens that have never existed in
`tokens.css` — `--color-border`, `--color-text-muted`, `--color-danger`, `--font-size-sm`,
`--color-warning-subtle` and friends, where the real names are `--border`, `--text-muted`,
`--danger`, `--text-sm`, `--warning-quiet`. The CSV import dialog was the worst of them: every
colour, every font size and the section dividers were dead, so the whole screen rendered as
unstyled default text. It reached a user as "the upload button looks wrong", which is how a
whole-file styling failure presents.

There is no frontend test runner here, so this reads the source — the same approach as the nav and
route-guard tests. It is a spelling check, not a design review: a reference with a fallback
(`var(--maybe, 8px)`) is fine by construction and is skipped.
"""
from __future__ import annotations

import pathlib
import re

STYLES = pathlib.Path("frontend/src/styles")
SRC = pathlib.Path("frontend/src")

#: `--name:` on the left of a colon anywhere in the global stylesheets. Definitions inside a
#: `[data-theme]` or a media block count — a token defined only in the dark theme is still defined.
_DEFINITION = re.compile(r"(--[a-zA-Z0-9-]+)\s*:")
#: `var(--name` NOT followed by a comma before the closing paren, i.e. no fallback.
_BARE_REFERENCE = re.compile(r"var\(\s*(--[a-zA-Z0-9-]+)\s*\)")


def _defined_tokens() -> set[str]:
    names: set[str] = set()
    for path in STYLES.glob("*.css"):
        names |= set(_DEFINITION.findall(path.read_text(encoding="utf-8")))
    return names


def test_the_global_stylesheets_are_where_tokens_live():
    """A guard on the guard. If `tokens.css` moves, this file silently starts checking against an
    empty set and passes everything."""
    defined = _defined_tokens()
    assert len(defined) > 40, f"only {len(defined)} tokens found — did tokens.css move?"
    for essential in ("--accent", "--border", "--text", "--text-muted", "--space-4", "--radius"):
        assert essential in defined, f"{essential} is missing from the token set"


def test_every_referenced_token_exists():
    """The check. A bare `var(--x)` naming a token nothing defines is a dead declaration."""
    defined = _defined_tokens()
    missing: dict[str, set[str]] = {}

    for path in sorted(SRC.rglob("*.css")):
        if path.parent == STYLES:
            continue
        text = path.read_text(encoding="utf-8")
        # Tokens a file defines for itself are legitimate.
        local = set(_DEFINITION.findall(text))
        for name in _BARE_REFERENCE.findall(text):
            if name not in defined and name not in local:
                missing.setdefault(str(path).replace("\\", "/"), set()).add(name)

    assert not missing, "stylesheets reference tokens that do not exist:\n" + "\n".join(
        f"  {path}: {', '.join(sorted(names))}" for path, names in sorted(missing.items())
    )
