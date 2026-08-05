# tests/test_hiring_subtype.py
"""M17 — what KIND of hiring this is.

"Acme has 12 open roles" is true of almost every growing company. What a rep can act on is the
change: they started hiring Security, or they are recruiting a VP, or plans stepped up sharply.

**Subtypes of `hiring`, not new signal kinds** (decision, 2026-08-05). The test that matters most
is therefore the boring one: `kind` stays `hiring`, so every existing INTENT_WEIGHT, play condition
and alert rule keeps working and nothing has to learn three new strings at once.

All three are month-over-month deltas, so the first crawl of an account has no subtype. Saying
nothing is the honest answer when there is no baseline — a rep repeats these claims to a prospect,
and being confidently wrong in a first sentence loses the conversation.
"""
from __future__ import annotations

import pytest

from nexus.ingestion import hiring


class P:
    """A posting, as the ATS normaliser produces it."""

    def __init__(self, title: str, department: str = "", url: str = ""):
        self.title = title
        self.department = department
        self.url = url


def snap(postings):
    return hiring.snapshot(postings)


ENG = [P("Software Engineer", "engineering") for _ in range(8)]
DESIGN = [P("Product Designer", "design") for _ in range(2)]
BASELINE = snap(ENG + DESIGN)          # 10 roles, engineering + design, all IC


# ---- seniority reading -------------------------------------------------------------------------

@pytest.mark.parametrize("title,level", [
    ("Chief Revenue Officer", "exec"),
    ("CTO", "exec"),
    ("Co-Founder", "exec"),
    ("VP Engineering", "vp"),
    ("Vice President, Sales", "vp"),
    ("Vice-President Operations", "vp"),
    # A plain "President" IS an exec — the lookbehind must exclude only the "Vice" form, not
    # break the exec match entirely.
    ("President", "exec"),
    ("SVP Marketing", "vp"),
    ("Director of Operations", "director"),
    ("Head of Security", "director"),
    ("Engineering Manager", "manager"),
    ("Staff Engineer", "manager"),
    ("Principal Designer", "manager"),
    ("Software Engineer", "ic"),
    ("Account Executive", "ic"),
    ("", "ic"),
])
def test_seniority_is_read_from_the_title(title, level):
    assert hiring.seniority_of(title) == level


def test_the_most_senior_match_wins():
    """'VP of Engineering' is leadership, not an engineer — order in the pattern list matters."""
    assert hiring.seniority_of("VP of Engineering Manager") == "vp"


# ---- no baseline, no guess ---------------------------------------------------------------------

def test_the_first_crawl_of_an_account_has_no_subtype():
    """There is nothing to compare against. Inventing a subtype here is a claim a rep repeats."""
    assert hiring.classify(snap(ENG), None) == (None, "")


def test_a_tiny_baseline_is_not_a_baseline():
    """A company with 2 open roles has no 'mix' — percentages off it are noise."""
    tiny = snap([P("Engineer", "engineering"), P("Designer", "design")])
    assert hiring.classify(snap(ENG * 4), tiny) == (None, "")


# ---- new_function -------------------------------------------------------------------------------

def test_a_department_appearing_from_nothing_is_a_new_function():
    current = snap(ENG + DESIGN + [P("Account Executive", "sales") for _ in range(3)])
    subtype, reason = hiring.classify(current, BASELINE)
    assert subtype == hiring.NEW_FUNCTION
    assert "Sales" in reason


def test_one_role_in_a_new_department_is_not_a_new_function():
    """One req is a backfill or an experiment; two is an intent to build."""
    current = snap(ENG + DESIGN + [P("Account Executive", "sales")])
    assert hiring.classify(current, BASELINE)[0] != hiring.NEW_FUNCTION


def test_a_department_that_merely_grew_is_not_new():
    current = snap(ENG + DESIGN + [P("Product Designer", "design") for _ in range(5)])
    assert hiring.classify(current, BASELINE)[0] != hiring.NEW_FUNCTION


# ---- seniority_shift ----------------------------------------------------------------------------

def test_leadership_hiring_is_a_seniority_shift():
    current = snap([
        P("VP Engineering", "engineering"),
        P("Head of Security", "engineering"),
        P("Director of Product", "product"),
        P("Software Engineer", "engineering"),
    ])
    subtype, reason = hiring.classify(current, BASELINE)
    assert subtype == hiring.SENIORITY_SHIFT
    assert "senior roles" in reason


def test_a_single_leadership_opening_is_not_a_shift():
    """One VP opening is a replacement far more often than a reorganisation."""
    current = snap(ENG + DESIGN + [P("VP Engineering", "engineering")])
    assert hiring.classify(current, BASELINE)[0] != hiring.SENIORITY_SHIFT


def test_a_company_that_was_already_senior_heavy_has_not_shifted():
    senior_baseline = snap([
        P("VP Engineering", "engineering"), P("Director of Sales", "sales"),
        P("Head of Ops", "ops"), P("Engineer", "engineering"),
    ])
    current = snap([
        P("VP Product", "product"), P("Director of Design", "design"),
        P("Head of Data", "data"), P("Engineer", "engineering"),
    ])
    assert hiring.classify(current, senior_baseline)[0] != hiring.SENIORITY_SHIFT


# ---- surge ---------------------------------------------------------------------------------------

def test_a_sharp_step_up_is_a_surge():
    current = snap([P("Software Engineer", "engineering") for _ in range(30)])
    subtype, reason = hiring.classify(current, BASELINE)
    assert subtype == hiring.SURGE
    assert "10 to 30" in reason


def test_ordinary_growth_is_not_a_surge():
    """Both a relative AND an absolute threshold, so 10 -> 13 never trips."""
    current = snap(ENG + DESIGN + [P("Software Engineer", "engineering") for _ in range(3)])
    assert hiring.classify(current, BASELINE)[0] is None


def test_a_small_company_doubling_is_not_a_surge():
    """4 -> 8 is a doubling and still only four extra reqs."""
    small = snap([P("Engineer", "engineering") for _ in range(4)])
    current = snap([P("Engineer", "engineering") for _ in range(8)])
    assert hiring.classify(current, small)[0] != hiring.SURGE


def test_shrinking_is_not_a_signal():
    assert hiring.classify(snap(ENG[:3]), BASELINE)[0] is None


# ---- priority ------------------------------------------------------------------------------------

def test_a_new_function_outranks_a_surge():
    """A new department names the team to sell to; a surge only says 'everything is growing'."""
    current = snap(
        ENG + DESIGN
        + [P("Account Executive", "sales") for _ in range(15)]
        + [P("Engineer", "engineering") for _ in range(10)]
    )
    assert hiring.classify(current, BASELINE)[0] == hiring.NEW_FUNCTION


# ---- failure posture -----------------------------------------------------------------------------

def test_classification_never_raises():
    for bad in ({}, {"count": "x"}, {"departments": None}, {"seniority": "nope"}):
        hiring.classify(bad, BASELINE)          # must not raise
        hiring.classify(BASELINE, bad)


def test_snapshot_handles_missing_fields():
    assert hiring.snapshot([P("", ""), P("Engineer")])["count"] == 2


# ---- the snapshot round-trip ---------------------------------------------------------------------

def test_the_snapshot_replaces_rather_than_accumulates():
    """A growing history on custom_fields would be a table pretending to be a column."""
    class Acct:
        custom_fields = {"ats_board": {"provider": "ashby", "token": "acme"}}

    acct = Acct()
    hiring.write_snapshot(acct, snap(ENG))
    hiring.write_snapshot(acct, snap(DESIGN))
    stored = hiring.read_snapshot(acct)
    assert stored["count"] == 2
    # And it must not clobber the ATS token that lives beside it.
    assert acct.custom_fields["ats_board"]["token"] == "acme"


def test_reading_a_missing_snapshot_is_none():
    class Acct:
        custom_fields = {}

    assert hiring.read_snapshot(Acct()) is None


# ---- the compatibility guarantee -----------------------------------------------------------------

def test_the_kind_is_still_hiring_and_the_vocabulary_is_unchanged():
    """THE point of choosing a subtype over new kinds. If a subtype ever leaks into SIGNAL_KINDS,
    every consumer has to learn it at once — which is the change this decision avoided."""
    from nexus.models.signal import SIGNAL_KINDS

    for subtype in hiring.HIRING_SUBTYPES:
        assert subtype not in SIGNAL_KINDS
    assert "hiring" in SIGNAL_KINDS


def test_scoring_weight_for_hiring_is_untouched():
    from nexus.agents.scoring import INTENT_WEIGHTS

    assert INTENT_WEIGHTS["hiring"] == 0.5


async def test_a_subtype_is_persisted_and_defaults_to_null(fresh_db):
    """Existing rows and every non-hiring source keep NULL, which reads as 'no finer grain'."""
    from nexus.ingestion.service import get_ingestion_service
    from nexus.ingestion.sources import RawSignal
    from nexus.models.account import Account
    from nexus.models.signal import SignalEvent
    from nexus.workers.tasks import tenant_session
    from tests.conftest import make_tenant

    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acct = Account(name="Acme", domain="acme.com")
        ts.add(acct)
        await ts.flush()
        await get_ingestion_service().ingest(ts, acct, [
            RawSignal(kind="hiring", subtype="new_function", source="ats",
                      title="Acme is hiring its first Sales roles", dedupe_key="h1"),
            RawSignal(kind="funding", source="web_news", title="Acme raises", dedupe_key="f1"),
        ])
        rows = {s.kind: s for s in await ts.list(SignalEvent)}

    assert rows["hiring"].subtype == "new_function"
    assert rows["funding"].subtype is None
