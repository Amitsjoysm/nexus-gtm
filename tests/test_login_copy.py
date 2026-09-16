# tests/test_login_copy.py
"""What the login screen calls the thing you are about to do.

Reported 2026-09-16: the sign-up side of the login page was headed "Create your workspace". That is
what the product does internally, not what a visitor came to do, and the button beside it already
said "Sign up" — so the page named the same action two ways.
"""
from __future__ import annotations

import pathlib

LOGIN = pathlib.Path("frontend/src/pages/LoginPage.tsx")


def test_the_sign_up_heading_says_sign_up():
    src = LOGIN.read_text(encoding="utf-8")
    assert "Create your workspace" not in src
    assert '"Sign up"' in src


def test_log_in_and_sign_up_are_a_switch_at_the_top():
    """Reported 2026-09-16: sign-up was a small "Create a workspace" link under the form, which a
    first-time visitor reads past on the way to the password box. It is now a Log in | Sign up
    switch above the form, and the old link is gone rather than duplicated."""
    src = LOGIN.read_text(encoding="utf-8")
    assert 'variant="segmented"' in src, "the Log in | Sign up switch is missing"
    assert '{ value: "login", label: "Log in" }' in src
    assert '{ value: "signup", label: "Sign up" }' in src
    assert src.index('variant="segmented"') < src.index("<form"), "the switch must sit above the form"
    for old in ("Create a workspace", "New to InfoJoy?", "Already have a workspace?"):
        assert f'"{old}"' not in src and f">{old}<" not in src, f"the old toggle link survives: {old}"
    # One verb for one action: the switch says Log in, so the button must too.
    assert '? "Sign in"' not in src


def test_a_tab_inside_a_form_does_not_submit_it():
    """A button's default type is submit. The switch sits beside the form today, but a tablist is a
    primitive and will end up inside one."""
    src = pathlib.Path("frontend/src/components/ui/Tabs.tsx").read_text(encoding="utf-8")
    assert 'type="button"' in src


def test_flipping_the_switch_does_not_move_the_page():
    """Reported 2026-09-16: "when we click on sign up or login the whole screen moves".

    Measured at 1366x768 the switch sat at 122px on Log in and 46px on Sign up, because the form card
    was vertically centred around a form ~150px taller. At 640px tall it also made the document
    scroll: a 10px scrollbar shoved the layout sideways and the left panel's headline dropped 41px.
    The page is now exactly the viewport, the form is anchored to the top, and only the form column
    scrolls, with its gutter always reserved."""
    import re

    css = pathlib.Path("frontend/src/pages/LoginPage.module.css").read_text(encoding="utf-8")
    # Declarations only: the comments explain the old `min-height` and must not count as using it.
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    page = css.split(".page {", 1)[1].split("}", 1)[0]
    form_col = css.split(".formCol {", 1)[1].split("}", 1)[0]
    assert "height: 100dvh" in page and "min-height" not in page, "the page can grow again"
    assert "align-items: flex-start" in form_col, "the form is re-centred on every flip"
    assert "overflow-y: auto" in form_col and "scrollbar-gutter: stable" in form_col
