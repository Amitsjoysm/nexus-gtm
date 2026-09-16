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
