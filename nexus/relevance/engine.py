"""The Relevance Engine.

Turns a tenant's GTM profile (ICP, value props, product context) into:
  * a deterministic ICP-fit score for an account (no LLM required), and
  * a compact context string injected into every agent prompt so outputs are grounded in
    *this* tenant's motion rather than being generic.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact
from nexus.models.relevance import RelevanceProfile
from nexus.models.signal import SignalEvent

DEFAULT_WEIGHTS = {"industry": 0.35, "size": 0.30, "geo": 0.15, "tech": 0.20}


@dataclass(slots=True)
class IcpFit:
    score: int  # 0..100
    reasons: list[str] = field(default_factory=list)
    breakdown: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class RelevanceContext:
    """Everything an agent needs to produce a tenant-grounded, prescriptive output."""

    icp_summary: str
    value_props: list[dict]
    product_context: str
    account_fit: IcpFit | None = None

    def to_prompt(self) -> str:
        vp_lines = "\n".join(
            f"  - {vp.get('name', 'Value prop')}: {vp.get('description', '')}"
            f" (solves: {', '.join(vp.get('pains_solved', [])) or 'n/a'})"
            for vp in self.value_props
        ) or "  - (none defined)"
        fit = ""
        if self.account_fit is not None:
            fit = (
                f"\nACCOUNT ICP FIT: {self.account_fit.score}/100"
                f" — {'; '.join(self.account_fit.reasons)}"
            )
        # The grounding every agent inherits. "Never invent value props" was already here and is
        # the right instinct; what it lacked was the specific fabrications that actually cost a
        # rep credibility on a live call — a made-up customer name, an invented percentage, a
        # claimed integration. Naming them is what makes the rule enforceable, and this product
        # has the real facts to hand, so omission is always available as the alternative.
        return (
            "You are a GTM co-pilot writing on behalf of a specific company, for an experienced "
            "SDR who will send or say your output to a real buyer.\n"
            "GROUNDING RULES:\n"
            "- Use only the facts in this context and in the request. Never invent a customer "
            "name, a metric, a percentage, a case study, an integration, or a product capability.\n"
            "- If a detail would make the copy stronger but is not given, leave it out. An email "
            "that says less is recoverable; one that states something untrue is not.\n"
            "- Never claim the buyer said or did something that is not in the context.\n"
            "- Prefer the specific fact you were given over a general claim you could invent.\n"
            f"PRODUCT CONTEXT:\n{self.product_context or '(none provided)'}\n"
            f"IDEAL CUSTOMER PROFILE: {self.icp_summary}\n"
            f"VALUE PROPOSITIONS:\n{vp_lines}{fit}"
        )


def _band_score(value: int | None, lo: int | None, hi: int | None) -> float:
    """1.0 inside [lo, hi]; decays linearly to 0 over one band-width outside; neutral if unset."""
    if value is None or (lo is None and hi is None):
        return 0.5
    lo = lo if lo is not None else 0
    hi = hi if hi is not None else 10**9
    if lo <= value <= hi:
        return 1.0
    width = max(hi - lo, 1)
    dist = (lo - value) if value < lo else (value - hi)
    return max(0.0, 1.0 - dist / width)


class RelevanceEngine:
    def score_icp_fit(
        self,
        profile: RelevanceProfile | None,
        account: Account,
        *,
        learned_weights: dict[str, float] | None = None,
    ) -> IcpFit:
        """Score an account against the ICP.

        Weight precedence (low → high): static :data:`DEFAULT_WEIGHTS` < per-tenant
        ``learned_weights`` (from the outcome-feedback loop) < explicit ``icp["weights"]`` set by
        the user. So learning nudges the defaults but never overrides a deliberate human choice.
        """
        if profile is None or not profile.icp:
            return IcpFit(score=50, reasons=["No ICP defined; neutral fit"], breakdown={})

        icp = profile.icp
        weights = {**DEFAULT_WEIGHTS, **(learned_weights or {}), **(icp.get("weights") or {})}
        reasons: list[str] = []
        sub: dict[str, float] = {}

        industries = [i.lower() for i in icp.get("industries", [])]
        if not industries:
            sub["industry"] = 0.5
        elif account.industry and account.industry.lower() in industries:
            sub["industry"] = 1.0
            reasons.append(f"industry '{account.industry}' is in ICP")
        else:
            sub["industry"] = 0.0
            reasons.append(f"industry '{account.industry or 'unknown'}' not in ICP")

        sub["size"] = _band_score(
            account.employee_count, icp.get("employee_min"), icp.get("employee_max")
        )
        if account.employee_count is not None and icp.get("employee_min") is not None:
            inside = sub["size"] >= 0.999
            reasons.append(
                f"headcount {account.employee_count} "
                f"{'within' if inside else 'outside'} target band"
            )

        countries = [c.lower() for c in icp.get("countries", [])]
        if not countries:
            sub["geo"] = 0.5
        elif account.country and account.country.lower() in countries:
            sub["geo"] = 1.0
            reasons.append(f"geo '{account.country}' in ICP")
        else:
            sub["geo"] = 0.0

        required_tech = [t.lower() for t in icp.get("required_tech", [])]
        owned = {t.lower() for t in (account.tech_stack or [])}
        if not required_tech:
            sub["tech"] = 0.5
        elif not owned:
            # UNKNOWN, not absent — and the distinction decides whether the account survives at all.
            # A freshly discovered account has an empty stack because nothing has enriched it yet,
            # and scoring that 0.0 (identical to a confirmed mismatch) put it under `min_fit` in
            # nexus/discovery/auto.py. So requiring any tech returned ZERO accounts, and the
            # enrichment that would have filled the field in never ran: the data that qualifies an
            # account is fetched *after* the gate it just failed. Reported by a tester 2026-08-27.
            #
            # Neutral matches how industry and geo already treat missing data, and how
            # `_passes_hard_filters` keeps an account whose headcount is None.
            sub["tech"] = 0.5
            reasons.append("tech stack unknown; not counted against fit")
        else:
            hits = [t for t in required_tech if t in owned]
            sub["tech"] = len(hits) / len(required_tech)
            if hits:
                reasons.append(f"uses {', '.join(hits)}")
            else:
                reasons.append(f"stack known, without {', '.join(required_tech)}")

        total_w = sum(weights[k] for k in sub) or 1.0
        score = round(sum(sub[k] * weights[k] for k in sub) / total_w * 100)
        return IcpFit(score=score, reasons=reasons or ["scored on available firmographics"],
                      breakdown=sub)

    def build_context(
        self,
        profile: RelevanceProfile | None,
        account: Account | None = None,
        contacts: list[Contact] | None = None,
        signals: list[SignalEvent] | None = None,
    ) -> RelevanceContext:
        icp_summary = "(not defined)"
        value_props: list[dict] = []
        product_context = ""
        if profile is not None:
            icp = profile.icp or {}
            parts = []
            if icp.get("industries"):
                parts.append("industries " + ", ".join(icp["industries"]))
            if icp.get("employee_min") or icp.get("employee_max"):
                parts.append(f"{icp.get('employee_min', 0)}–{icp.get('employee_max', '∞')} employees")
            if icp.get("countries"):
                parts.append("in " + ", ".join(icp["countries"]))
            icp_summary = "; ".join(parts) or "(not defined)"
            value_props = profile.value_props or []
            product_context = profile.product_context or ""

        fit = self.score_icp_fit(profile, account) if account is not None else None
        return RelevanceContext(
            icp_summary=icp_summary,
            value_props=value_props,
            product_context=product_context,
            account_fit=fit,
        )


async def get_profile(ts: TenantSession) -> RelevanceProfile | None:
    return await ts.first(RelevanceProfile)


async def get_or_create_profile(ts: TenantSession) -> RelevanceProfile:
    profile = await ts.first(RelevanceProfile)
    if profile is None:
        profile = RelevanceProfile(tenant_id=ts.tenant_id, icp={}, value_props=[], product_context="")
        ts.add(profile)
        await ts.flush()
    return profile


_engine = RelevanceEngine()


def get_relevance_engine() -> RelevanceEngine:
    return _engine
