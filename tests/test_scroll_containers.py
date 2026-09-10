"""Every scroll container must be a containing block, or absolutely-positioned content escapes it.

Found 2026-09-10 from a screenshot of blank space below the whole app after "Generate brief" then
"Ask about this account", with two scrollbars on screen. Measured in the running app: the document
grew from 855px to 2,309px, and the one element responsible was

    <label class="sr-only">Your question</label>          offsetParent: BODY, top: 2308px

the screen-reader label on the Ask box. `.sr-only` is `position: absolute` with no offsets, so it
sits at its static position in the flow — far down a long page — but its containing block was the
document, because neither `.main` (the content scroller) nor anything above it was positioned. An
absolute element is clipped and scrolled only by overflow ancestors that lie between it and its
containing block, so it escaped `.main` AND the shell's `overflow: hidden`, stretched the document,
and gave the window a second scrollbar. Scrolling that pushes the entire app up and reveals the
empty body underneath.

The earlier `100vh -> 100dvh` change was aimed at a different cause (retracting mobile toolbars)
and could never have fixed this; the report came back unchanged.

It is a CLASS of bug, not one label: `.sr-only` is used by `Field` (every `hideLabel` input),
`DataTable` and `Spinner`, and the same escape from a NESTED scroller (the chat transcript) leaks
into `.main`'s own scroll height instead — the "blank span inside the main scroller" variant. So
this pins the invariant for every scroll container in every CSS module, which there is no frontend
test runner to check any other way.
"""
from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "src"

# `overflow`, `overflow-y` or `overflow-x` set to a value that makes a scroll container a user can
# scroll. `hidden` is deliberately excluded: it is used all over for text truncation, and requiring
# a containing block there would be noise.
_SCROLLS = re.compile(r"overflow(?:-[xy])?\s*:\s*(?:auto|scroll)\b")
_POSITIONED = re.compile(r"position\s*:\s*(?:relative|absolute|fixed|sticky)\b")
# An innermost `selector { declarations }` block. Nested @media blocks are handled because the
# pattern only ever matches a block that contains no further braces.
_BLOCK = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _class_names(selector: str) -> set[str]:
    return set(re.findall(r"\.([A-Za-z_][\w-]*)", selector))


def _unpositioned_scrollers(css: str) -> set[str]:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    scrolls: set[str] = set()
    positioned: set[str] = set()
    for selector, body in _BLOCK.findall(css):
        # The LAST class in a compound selector is the element being styled: `.errors ul` styles
        # the `ul`, `.rail[data-open]` styles `.rail`. Descendant element selectors are keyed on
        # the whole selector so they cannot be satisfied by an unrelated rule.
        key = selector.strip()
        names = _class_names(key)
        target = key if re.search(r"\s[a-z]+\s*$", key) else (sorted(names)[-1] if names else key)
        if _SCROLLS.search(body):
            scrolls.add(target)
        if _POSITIONED.search(body):
            positioned.add(target)
    return scrolls - positioned


def test_every_scroll_container_is_a_containing_block():
    offenders = []
    for path in sorted(FRONTEND.rglob("*.module.css")):
        for sel in sorted(_unpositioned_scrollers(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(FRONTEND)}  {sel}")
    assert not offenders, (
        "scroll containers without a containing block — an absolutely-positioned descendant (every "
        "`.sr-only` label) escapes them and stretches the page or the scroller above:\n  "
        + "\n  ".join(offenders)
    )


def test_the_main_content_scroller_contains_its_own_content():
    """The one that produced the screenshot, pinned by name so the reason survives."""
    css = (FRONTEND / "components" / "layout" / "AppShell.module.css").read_text(encoding="utf-8")
    main = re.search(r"\.main\s*\{([^}]*)\}", css)
    assert main and _POSITIONED.search(main.group(1)), (
        "`.main` is not positioned: an `.sr-only` label at the bottom of a long page escapes it and "
        "gives the window a second scrollbar with blank space below the app"
    )


def test_the_detector_catches_the_real_bug_and_accepts_the_fix():
    """The rule has to fail on the shape that shipped, or it proves nothing."""
    broken = ".main { flex: 1; overflow-y: auto; }"
    fixed = ".main { flex: 1; position: relative; overflow-y: auto; }"
    in_media = ".rail { position: relative; } @media (max-width: 1024px) { .rail { overflow: auto; } }"
    assert _unpositioned_scrollers(broken) == {"main"}
    assert _unpositioned_scrollers(fixed) == set()
    assert _unpositioned_scrollers(in_media) == set()
    assert _unpositioned_scrollers(".truncate { overflow: hidden; }") == set()
