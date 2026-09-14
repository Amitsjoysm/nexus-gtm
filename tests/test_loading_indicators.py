"""A loading indicator never freezes, and a long action says it is still working.

Reported: spinners "get stuck and don't move" on Find lookalikes and Find similar. Measured in
headless Chromium against the live bundle with frames confirmed painting (62 fps): with motion
allowed the ring turns normally; with ``prefers-reduced-motion: reduce`` its computed animation is
``1e-05s`` for ONE iteration, the browser drops it, and the ring is a still image. The cause is the
blanket rule in ``global.css`` — ``animation-duration: 0.01ms !important`` and
``animation-iteration-count: 1 !important`` on ``*`` — which beat ``Spinner.module.css``'s own
reduced-motion rule because that one was not ``!important``. Reduced motion is on for Remote
Desktop sessions, for Windows "Animation effects" off, and for macOS "Reduce motion".

A loading indicator is essential motion: stopping it removes the only sign that work is happening.
It slows down under reduced motion; it does not stop.

There is no frontend test runner, so — as elsewhere in this suite — these read the source.
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "frontend" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


def _reduced_motion_block(css: str) -> str:
    """Every ``@media (prefers-reduced-motion: reduce) { ... }`` body in a stylesheet, joined."""
    bodies = []
    for match in re.finditer(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{", css):
        depth, i = 1, match.end()
        while depth and i < len(css):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        bodies.append(css[match.end(): i - 1])
    return "\n".join(bodies)


def _keeps_turning(block: str, selector: str) -> None:
    rule = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", block)
    assert rule, f"{selector} has no reduced-motion rule, so the global one stops it"
    body = rule.group(1)
    assert re.search(r"animation-iteration-count:\s*infinite\s*!important", body), (
        f"{selector} must stay infinite under reduced motion, !important to beat the global `*` rule"
    )
    duration = re.search(r"animation-duration:\s*([\d.]+)(m?s)\s*!important", body)
    assert duration, f"{selector} needs an !important duration under reduced motion"
    seconds = float(duration.group(1)) / (1000 if duration.group(2) == "ms" else 1)
    assert seconds >= 0.5, f"{selector} reduced-motion duration {seconds}s is not a real rotation"


def test_the_global_rule_is_what_stops_animations():
    """Pins the premise: if this rule goes, the overrides below are no longer needed."""
    block = _reduced_motion_block(_read("styles/global.css"))
    assert "animation-iteration-count: 1 !important" in block


def test_a_spinner_keeps_turning_under_reduced_motion():
    _keeps_turning(_reduced_motion_block(_read("components/ui/Spinner.module.css")), ".spinner")


def test_the_progress_bar_keeps_moving_under_reduced_motion():
    _keeps_turning(
        _reduced_motion_block(_read("components/ui/WorkingIndicator.module.css")), ".bar"
    )


def test_long_actions_show_progress_instead_of_a_bare_spinner():
    """Find lookalikes, Find similar people, Find contacts and the AI actions run for tens of
    seconds. A 16px spinner beside static text is what reads as a hang."""
    expected_uses = {
        "pages/AccountDetailPage.tsx": 4,   # lookalikes, AI actions, similar people, find contacts
        "pages/ContactsPage.tsx": 1,        # similar people
        "components/EmailComposer.tsx": 1,  # drafting the email
    }
    for rel, count in expected_uses.items():
        src = _read(rel)
        assert src.count("<WorkingIndicator") >= count, f"{rel} lost a progress indicator"
        assert "<Spinner" not in src, f"{rel} still shows a bare spinner for a long action"


def test_the_indicator_counts_time_and_says_when_it_is_slow():
    src = _read("components/ui/WorkingIndicator.tsx")
    assert "setInterval" in src, "the elapsed time must tick"
    assert "slowAfter" in src, "a long wait must be acknowledged, not left spinning in silence"
    # One live region. The counter changes every second and must not be read out every second.
    assert src.count('role="status"') == 1
    elapsed = src[src.index("styles.elapsed") - 200: src.index("styles.elapsed") + 80]
    assert 'aria-hidden="true"' in elapsed, "the ticking counter must be hidden from screen readers"
    assert "decorative" in src, "the inner spinner must not be a second live region"


def test_a_decorative_spinner_is_not_announced():
    src = _read("components/ui/Spinner.tsx")
    assert "decorative" in src
    assert 'aria-hidden="true"' in src
