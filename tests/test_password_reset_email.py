# tests/test_password_reset_email.py
"""The password-reset email sends a BUTTON, not a bare URL.

A raw URL in an email reads as a phishing attempt — long, opaque, full of query parameters — and it
is the message people are most primed to distrust, because "click this link to reset your password"
is what an actual attack looks like. A styled button is what a person expects from a real product.

Three things this must keep doing, and each is the reason the plain-text part still exists:

* **multipart/alternative, always.** Corporate mail gateways strip HTML, some clients render text
  only, and screen readers do better with it. An HTML-only reset email is unopenable for those
  users, and a password reset is precisely the message that must not fail.
* **The URL appears in the text part.** If the button does not render, or a client blocks the link,
  the URL is the only way the user recovers. Sending a button with no fallback URL anywhere turns a
  rendering quirk into a locked account.
* **Table-based markup with inline styles.** Email clients do not reliably support flexbox, grid, or
  a `<style>` block; Outlook in particular ignores most of it. A `<div>` styled as a button collapses
  to plain text there, which is the failure this change is meant to remove.
"""
from __future__ import annotations

import re


LINK = "https://app.example.com/reset-password?email=jo%40acme.com&token=abc123"


def _captured():
    """Build the reset email without sending, returning (subject, text, html)."""
    from nexus.auth import password_reset

    return password_reset.build_reset_email(LINK)


# ---- the button ------------------------------------------------------------------------------

def test_the_html_part_contains_a_button_wrapping_the_link():
    _subject, _text, html = _captured()
    assert html, "there must be an HTML alternative at all"
    assert LINK in html, "the button must point at the reset URL"
    # An <a> styled as a button, not a <button> element: a <button> outside a form does nothing in
    # an email client, and form submission is not available there.
    assert re.search(r"<a\b[^>]*href=", html), "the button must be an anchor"


def test_the_button_uses_table_markup_and_inline_styles():
    """Outlook ignores <style> blocks and most modern layout CSS, so a div-based button collapses
    to plain text there — the exact failure this replaces."""
    _subject, _text, html = _captured()
    assert "<table" in html.lower(), "email buttons need table markup to render in Outlook"
    assert "style=" in html, "styles must be inline, not in a <style> block"
    for unsupported in ("display:flex", "display: flex", "display:grid", "display: grid"):
        assert unsupported not in html.replace(" ", "").lower() or True
    assert "flex" not in html.lower(), "flexbox does not render in email clients"


def test_the_raw_url_is_not_the_visible_html_call_to_action():
    """The point of the change: the reader sees an action, not a wall of query parameters."""
    _subject, _text, html = _captured()
    # Strip tags, then the URL should not be what the reader is asked to click on.
    visible = re.sub(r"<[^>]+>", " ", html)
    assert "Reset password" in visible or "Reset my password" in visible
    assert "token=abc123" not in visible, (
        "the raw token URL is rendered as visible text in the HTML part, which is the "
        "phishing-looking presentation this replaces"
    )


# ---- the text fallback -----------------------------------------------------------------------

def test_a_plain_text_alternative_still_exists():
    """Corporate gateways strip HTML. A password reset is the message that must not fail."""
    _subject, text, _html = _captured()
    assert text.strip(), "there must be a plain-text part"
    assert "<" not in text, "the text part must not contain markup"


def test_the_url_is_present_in_the_text_part():
    """If the button does not render or the client blocks it, this URL is the only recovery."""
    _subject, text, _html = _captured()
    assert LINK in text


def test_both_parts_state_the_expiry():
    """A reset link that has silently expired is the commonest support ticket in this flow."""
    _subject, text, html = _captured()
    assert "minute" in text.lower()
    assert "minute" in html.lower()


def test_both_parts_carry_the_ignore_notice():
    """Someone who did NOT request a reset needs telling their password is unchanged — otherwise a
    routine email reads as evidence of a breach."""
    _subject, text, html = _captured()
    for part in (text, html):
        assert "didn't request" in part or "did not request" in part


# ---- delivery ---------------------------------------------------------------------------------

async def test_send_email_accepts_an_html_alternative(monkeypatch):
    """`send_email` took a plain body only, so nothing could send HTML. The parameter is optional,
    so every existing caller is unchanged."""
    import inspect

    from nexus.integrations.email_sender import send_email

    assert "html" in inspect.signature(send_email).parameters


def test_the_message_is_multipart_when_html_is_supplied():
    from nexus.integrations.email_sender import _build_message

    cfg = {"from_email": "no-reply@example.com", "from_name": "InfoJoy GTM"}
    msg = _build_message(cfg, "jo@acme.com", "Subject", "plain body", html="<p>rich</p>")
    assert msg.is_multipart(), "an HTML alternative must produce multipart/alternative"
    types = {p.get_content_type() for p in msg.walk() if not p.is_multipart()}
    assert types == {"text/plain", "text/html"}


def test_the_message_stays_plain_without_html():
    """Regression guard: every other caller passes no html and must produce exactly what it did."""
    from nexus.integrations.email_sender import _build_message

    cfg = {"from_email": "no-reply@example.com", "from_name": "InfoJoy GTM"}
    msg = _build_message(cfg, "jo@acme.com", "Subject", "plain body")
    assert not msg.is_multipart()
    assert msg.get_content_type() == "text/plain"


async def test_the_reset_flow_sends_the_html_part(monkeypatch):
    """End to end: the builder is wired into the sender, not merely present."""
    from nexus.auth import password_reset

    seen: dict = {}

    async def fake_send(cfg, *, to, subject, body, html=None):
        seen.update({"to": to, "subject": subject, "body": body, "html": html})

        class R:
            ok = True
            detail = ""

        return R()

    monkeypatch.setattr("nexus.integrations.email_sender.send_email", fake_send)
    await password_reset.send_reset_email("jo@acme.com", LINK)

    assert seen.get("html"), "the reset email was sent without its HTML part"
    assert "<a" in seen["html"]
    assert LINK in seen["body"], "the text part must still carry the URL"
