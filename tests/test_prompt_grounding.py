# tests/test_prompt_grounding.py
"""Everything we fetched about an account has to reach the prompt.

Audited 2026-09-01 after a user reported the agents writing generic copy. The messaging agent's
prompt carried **`ctx.account.name` and nothing else** about the company, and exactly ONE signal --
its `title`, never its `body`.

So the model writing a cold email did not know whether it was addressing a 40-person fintech or a
6,000-person manufacturer, what stack they run, or what the signal actually said. We crawl a funding
announcement, store "raised $40M Series B led by Sequoia to expand European operations" in
`signal.body`, and send the model the headline "Vanta raises Series B". The substance -- the part a
rep would actually open on -- was fetched, stored, billed for, and then dropped on the floor.

That is the difference between a mail-merge and personalisation, and it is why the output read as
generic even for accounts we knew a great deal about.

Two rules the builders here exist to enforce:

* **Only known facts are rendered.** A blank or unknown field is OMITTED, never rendered as
  "unknown" or "N/A". A line reading "Employees: unknown" invites the model to write around a hole;
  an absent line simply gives it less to work with, which is the honest state.
* **The value proposition is CHOSEN, not always the first one.** Pitching `value_props[0]` at every
  account regardless of what triggered the outreach is the mail-merge failure in its purest form.
"""
from __future__ import annotations

from datetime import timedelta

from nexus.core.db import utcnow
from nexus.models.account import Account
from nexus.models.signal import SignalEvent


def _signal(**kw):
    kw.setdefault("tenant_id", "t1")
    kw.setdefault("account_id", "a1")
    kw.setdefault("source", "web")
    kw.setdefault("dedupe_key", kw.get("title", "k"))
    kw.setdefault("occurred_at", utcnow())
    return SignalEvent(**kw)


# ---- account facts ----------------------------------------------------------------------------

def test_known_firmographics_are_rendered():
    from nexus.agents.copy import account_facts

    out = account_facts(Account(
        tenant_id="t1", name="Acme", domain="acme.com", industry="Fintech",
        employee_count=120, country="United States", region="California",
        tech_stack=["Salesforce", "Snowflake"],
    ))
    # A BAND, not the raw headcount. "120 employees" invites the model to quote the number back at
    # the buyer -- which reads as surveillance and is usually stale by the time the email lands.
    # The band is what actually changes how you write; the precise figure is not.
    for expected in ("Fintech", "50-200", "California", "Salesforce", "Snowflake"):
        assert expected in out, f"{expected!r} missing from: {out!r}"
    assert "120" not in out, "the raw headcount must not be handed to the model"


def test_unknown_fields_are_omitted_not_labelled_unknown():
    """'Employees: unknown' invites the model to write around a hole. An absent line just gives it
    less, which is the truth."""
    from nexus.agents.copy import account_facts

    out = account_facts(Account(tenant_id="t1", name="Acme", domain="acme.com")).lower()
    assert "unknown" not in out
    assert "n/a" not in out
    assert "none" not in out


def test_an_account_with_nothing_known_renders_empty():
    """No facts must produce no section at all, so the caller can skip it rather than emit a
    heading with nothing under it."""
    from nexus.agents.copy import account_facts

    assert account_facts(Account(tenant_id="t1", name="Acme")).strip() in ("", "Company: Acme")


def test_the_tech_stack_is_capped():
    """A 40-item stack would crowd out the signal, which is the more perishable fact."""
    from nexus.agents.copy import account_facts

    out = account_facts(Account(
        tenant_id="t1", name="Acme", tech_stack=[f"tool{i}" for i in range(40)],
    ))
    assert out.count("tool") <= 8


# ---- signal rendering -------------------------------------------------------------------------

def test_the_signal_body_reaches_the_prompt():
    """THE bug. The body is the substance; the title is a headline."""
    from nexus.agents.copy import signal_facts

    out = signal_facts([_signal(
        kind="funding", title="Acme raises Series B",
        body="Acme raised $40M led by Sequoia to expand European operations.",
        strength=0.9,
    )])
    assert "$40M" in out
    assert "Sequoia" in out


def test_several_signals_are_rendered_strongest_first():
    """One signal was all the model ever saw. Two related events are the material a rep actually
    opens on -- 'you raised, and you're hiring six SREs' is a better email than either alone."""
    from nexus.agents.copy import signal_facts

    out = signal_facts([
        _signal(kind="hiring", title="Hiring 6 SREs", strength=0.6, dedupe_key="h"),
        _signal(kind="funding", title="Raised Series B", strength=0.95, dedupe_key="f"),
    ])
    assert out.index("Series B") < out.index("SREs"), "strongest signal must lead"


def test_signal_rendering_is_bounded():
    """An account with 50 stored signals must not swamp the prompt -- the token budget would trim
    it, and what gets trimmed is not something we get to choose."""
    from nexus.agents.copy import signal_facts

    many = [_signal(kind="news", title=f"Story {i}", strength=0.5, dedupe_key=str(i))
            for i in range(50)]
    assert len(signal_facts(many, limit=3).strip().splitlines()) <= 6


def test_a_long_body_is_trimmed_not_dropped():
    from nexus.agents.copy import signal_facts

    out = signal_facts([_signal(
        kind="news", title="Long one", body="x" * 5000, strength=0.9,
    )])
    assert len(out) < 1200
    assert "Long one" in out


def test_no_signals_renders_empty():
    from nexus.agents.copy import signal_facts

    assert signal_facts([]).strip() == ""


def test_stale_signals_are_labelled_with_their_age():
    """A rep opening on a nine-month-old funding round sounds like they just found it. The model
    needs to know how fresh the fact is to phrase it honestly."""
    from nexus.agents.copy import signal_facts

    out = signal_facts([_signal(
        kind="funding", title="Raised Series A", strength=0.9,
        occurred_at=utcnow() - timedelta(days=270),
    )])
    assert any(tok in out.lower() for tok in ("month", "ago", "2026", "202"))


# ---- value proposition selection ---------------------------------------------------------------

def test_the_value_prop_is_matched_to_the_signal():
    """Pitching value_props[0] at every account regardless of what triggered the outreach IS the
    mail-merge failure. A hiring signal should pull the value prop about headcount cost, not
    whichever one happens to be first."""
    from nexus.agents.copy import select_value_prop

    vps = [
        {"name": "Audit automation", "pains_solved": ["manual evidence collection"]},
        {"name": "Onboarding speed", "pains_solved": ["slow ramp for new engineering hires"]},
    ]
    chosen = select_value_prop(vps, [_signal(
        kind="hiring", title="Hiring 12 engineers",
        body="Acme is hiring 12 engineers to scale its platform team.", strength=0.8,
    )])
    assert chosen["name"] == "Onboarding speed"


def test_selection_falls_back_to_the_first_when_nothing_matches():
    """Never return nothing: a value prop is required to write at all."""
    from nexus.agents.copy import select_value_prop

    vps = [{"name": "Audit automation", "pains_solved": ["evidence collection"]}]
    chosen = select_value_prop(vps, [_signal(kind="news", title="Opened an office", strength=0.4)])
    assert chosen["name"] == "Audit automation"


def test_selection_with_no_value_props_returns_a_placeholder():
    """The caller already handles the unconfigured workspace; this must not raise on the way."""
    from nexus.agents.copy import select_value_prop

    assert select_value_prop([], []) .get("name")


def test_selection_with_no_signals_returns_the_first():
    from nexus.agents.copy import select_value_prop

    vps = [{"name": "First"}, {"name": "Second"}]
    assert select_value_prop(vps, [])["name"] == "First"
