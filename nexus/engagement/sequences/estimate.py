"""What a campaign can cost, and whether the workspace can cover it before launch (spec §10, D18).

**The worst case is the gate, and it is the literal worst case**: every contact receives every email
step, each one drafted and sent, and every contact replies once, is classified and gets a suggested
response. A campaign that launches on an expected cost runs dry in week three, and a buyer who was
half-way through a sequence goes quiet for reasons they never see.

**The likely cost is shown for information.** It assumes each follow-up reaches the 85% of people
who have not answered yet, and 15% reply — a stated heuristic, not a forecast, and it gates nothing.

Prices come from the live rate cards through the same `_usage_amount` the meter charges with, so the
number on the launch screen is the number that is spent. The gate applies in `shadow` and `on`
billing modes (D18); it is skipped when billing is `off` and for unlimited plan classes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

DRAFT = "ai.email_draft"
SEND = "outreach.email_send"
CLASSIFY = "ai.reply_classify"
REPLY_DRAFT = "ai.reply_draft"

#: The share of people who have not answered by the next follow-up, and who reply at all.
CONTINUE_RATE = 0.85
REPLY_RATE = 0.15


@dataclass(slots=True)
class Estimate:
    contacts: int
    email_steps: int
    worst_credits: float
    likely_credits: float
    balance: float
    gate_applies: bool
    covered: bool
    shortfall: float
    per_capability: dict

    def as_dict(self) -> dict:
        return asdict(self)


def worst_units(contacts: int, email_steps: int) -> dict[str, float]:
    sends = contacts * email_steps
    return {DRAFT: sends, SEND: sends, CLASSIFY: contacts, REPLY_DRAFT: contacts}


def likely_units(contacts: int, email_steps: int) -> dict[str, float]:
    sends = sum(contacts * CONTINUE_RATE ** i for i in range(email_steps))
    replies = contacts * REPLY_RATE
    return {DRAFT: sends, SEND: sends, CLASSIFY: replies, REPLY_DRAFT: replies}


async def estimate(ts, campaign) -> Estimate:
    from nexus.billing.credits import balance
    from nexus.billing.entitlements import _usage_amount, resolve_entitlement
    from nexus.core.config import get_settings
    from nexus.models.engagement import EngagementEnrollment, EngagementStep

    steps = await ts.list(EngagementStep, EngagementStep.campaign_id == campaign.id)
    email_steps = sum(1 for s in steps if s.channel == "email")
    enrollments = await ts.list(EngagementEnrollment,
                                EngagementEnrollment.campaign_id == campaign.id)
    contacts = sum(1 for e in enrollments if e.status not in ("stopped",))

    worst = worst_units(contacts, email_steps)
    likely = likely_units(contacts, email_steps)
    per: dict[str, dict] = {}
    worst_total = likely_total = 0.0
    unlimited = False
    for capability, quantity in worst.items():
        ent = await resolve_entitlement(ts, capability)
        unlimited = unlimited or ent.mode == "unlimited"
        worst_cost = float(await _usage_amount(ts, ent, quantity) or 0) if quantity else 0.0
        likely_cost = float(await _usage_amount(ts, ent, likely[capability]) or 0) \
            if likely[capability] else 0.0
        per[capability] = {"units": quantity, "credits": worst_cost,
                           "likely_units": round(likely[capability], 1),
                           "likely_credits": round(likely_cost, 2)}
        worst_total += worst_cost
        likely_total += likely_cost

    mode = get_settings().billing_enforcement
    gate_applies = mode in ("shadow", "on") and not unlimited
    available = float(await balance(ts))
    covered = (not gate_applies) or available >= worst_total
    return Estimate(
        contacts=contacts, email_steps=email_steps, worst_credits=round(worst_total, 2),
        likely_credits=round(likely_total, 2), balance=round(available, 2),
        gate_applies=gate_applies, covered=covered,
        shortfall=round(max(0.0, worst_total - available), 2) if gate_applies else 0.0,
        per_capability=per,
    )
