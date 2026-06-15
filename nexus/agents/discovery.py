# nexus/agents/discovery.py
"""Discovery Agent — surface companies (or contacts) matching a conversation's ICP.

Read-only by design: it ranks the tenant's own accounts by deterministic ICP fit, then fills any
gap from the web (deduped by domain, persisted as ``source="discovery"`` so the results table can
tell own data from net-new). The conversation's ICP — not the saved RelevanceProfile — drives the
scoring, so discovery reflects exactly what the rep asked for. Fully offline: scoring needs no LLM
and the browser returns no hits in tests, making counts and ordering reproducible.
"""
from __future__ import annotations

from nexus.agents.runtime import AgentContext, BaseAgent, register_agent
from nexus.core.config import get_settings
from nexus.models.account import Account, Contact
from nexus.models.relevance import RelevanceProfile


def _icp_to_scoring(icp: dict) -> dict:
    """Map conversational ICP slots onto the RelevanceEngine's profile.icp keys."""
    size = icp.get("company_size") or {}
    return {
        "industries": icp.get("industries", []),
        "employee_min": size.get("min"),
        "employee_max": size.get("max"),
        "countries": icp.get("geo", []),
        "required_tech": icp.get("required_tech", []),
    }


def _passes_hard_filters(account: Account, scoring_icp: dict) -> bool:
    """Hard-exclude only on the explicit numeric size band the user stated.

    Size is a precise, user-asserted bound, so an account whose known headcount falls
    outside it is a definitive non-match and is dropped. Industry and geo are deliberately
    *not* hard filters: CRM industry/country labels are inconsistent ("Financial Services"
    vs "Fintech", "US" vs "United States"), so exact-string exclusion would hide good but
    mislabeled accounts. They instead drive the ICP-fit ranking — near-matches still surface,
    ranked lower, where the rep can see and refine them. Unknown headcount (None) is kept.
    """
    lo, hi = scoring_icp.get("employee_min"), scoring_icp.get("employee_max")
    if account.employee_count is not None:
        if lo is not None and account.employee_count < lo:
            return False
        if hi is not None and account.employee_count > hi:
            return False
    return True


class DiscoveryAgent(BaseAgent):
    name = "discovery"

    async def run(self, ctx: AgentContext) -> dict:
        icp = ctx.inputs.get("icp") or {}
        target = ctx.inputs.get("target") or "companies"
        max_candidates = int(
            ctx.inputs.get("max_candidates") or get_settings().discovery_max_candidates
        )
        scoring_icp = _icp_to_scoring(icp)
        # Transient (unsaved) profile: scores against the conversation's ICP, not the saved one.
        profile = RelevanceProfile(tenant_id=ctx.ts.tenant_id, icp=scoring_icp,
                                   value_props=[], product_context="")

        if target == "contacts":
            return await self._discover_contacts(ctx, profile, scoring_icp, icp, max_candidates)
        return await self._discover_companies(ctx, profile, scoring_icp, icp, max_candidates)

    async def _discover_companies(
        self, ctx: AgentContext, profile, scoring_icp: dict, icp: dict, max_candidates: int
    ) -> dict:
        accounts = await ctx.ts.list(Account)
        survivors = [a for a in accounts if _passes_hard_filters(a, scoring_icp)]
        scored = []
        for a in survivors:
            fit = ctx.relevance.score_icp_fit(profile, a)
            scored.append((fit, a))
        scored.sort(key=lambda t: (-t[0].score, t[1].name))
        own = scored[:max_candidates]
        candidates = [self._candidate(a, fit, source="own", is_new=False) for (fit, a) in own]
        seen_domains = {a.domain for (_, a) in own if a.domain}

        new_count = 0
        if len(candidates) < max_candidates and ctx.registry is not None:
            need = max_candidates - len(candidates)
            # The registry runs the company-search waterfall (InfoJoy -> web -> Apify) and
            # returns net-new candidates deduped/merged by domain with per-field provenance.
            found = await ctx.registry.company_search(icp, limit=need)
            for cand in found:
                domain = (cand.domain or "").strip().lower()
                if not domain or domain in seen_domains:
                    continue
                existing = await ctx.ts.first(Account, Account.domain == domain)
                if existing is not None:
                    continue
                acc = Account(
                    tenant_id=ctx.ts.tenant_id,
                    name=(cand.name or domain).strip(),
                    domain=domain,
                    industry=cand.industry or (icp.get("industries") or [None])[0],
                    country=cand.country or (icp.get("geo") or [None])[0],
                    employee_count=cand.employee_count,
                    source="discovery",
                )
                ctx.ts.add(acc)
                await ctx.ts.flush()
                fit = ctx.relevance.score_icp_fit(profile, acc)
                candidates.append(self._candidate(acc, fit, source="discovery", is_new=True))
                seen_domains.add(domain)
                new_count += 1
                if len(candidates) >= max_candidates:
                    break

        return {
            "target": "companies",
            "counts": {"own": len(own), "new": new_count},
            "candidates": candidates,
        }

    async def _discover_contacts(
        self, ctx: AgentContext, profile, scoring_icp: dict, icp: dict, max_candidates: int
    ) -> dict:
        """Rank the tenant's own contacts by their account's ICP fit, then fill the gap with
        net-new buying-committee personas sourced for the best-fit accounts that haven't already
        surfaced a contact. Sourced personas are persisted ``enrichment_source="sourcing:<prov>"``
        so they're marked as sourced and stay gated from real outreach until a verified email is
        found — discovery surfaces who to target; the cadence/campaign send-bar still applies."""
        contacts = await ctx.ts.list(Contact)
        scored = []
        for c in contacts:
            acc = await ctx.ts.get(Account, c.account_id)
            if acc is None:
                continue
            fit = ctx.relevance.score_icp_fit(profile, acc)
            scored.append((fit, c, acc))
        scored.sort(key=lambda t: (-t[0].score, t[1].full_name))
        top = scored[:max_candidates]
        candidates = [self._contact_candidate(c, acc, fit, source="own", is_new=False)
                      for (fit, c, acc) in top]
        seen_accounts = {c.account_id for (_, c, _) in top}

        new_count = 0
        if len(candidates) < max_candidates and ctx.registry is not None:
            # The stub/real providers key the persona title off ``buyer_titles``; the
            # conversational ICP stores it under ``titles`` — bridge so the rep gets the role
            # they asked for ("VP Sales") rather than a generic "Decision Maker".
            search_icp = dict(icp)
            if not search_icp.get("buyer_titles") and icp.get("titles"):
                search_icp["buyer_titles"] = icp["titles"]

            per_account = max(1, get_settings().discovery_contacts_per_account)
            accounts = await ctx.ts.list(Account)
            survivors = [a for a in accounts if _passes_hard_filters(a, scoring_icp)]
            acc_scored = sorted(
                ((ctx.relevance.score_icp_fit(profile, a), a) for a in survivors),
                key=lambda t: (-t[0].score, t[1].name),
            )
            for fit, acc in acc_scored:
                if len(candidates) >= max_candidates:
                    break
                if acc.id in seen_accounts:
                    continue
                # Source the buying committee for this account (up to per_account people).
                found = await ctx.registry.contact_search(acc, search_icp, limit=per_account)
                for cand in found:
                    if len(candidates) >= max_candidates:
                        break
                    person = Contact(
                        tenant_id=ctx.ts.tenant_id,
                        account_id=acc.id,
                        full_name=cand.full_name,
                        title=cand.title,
                        seniority=cand.seniority,
                        email=cand.email,
                        enrichment_source=f"sourcing:{cand.source}",
                    )
                    ctx.ts.add(person)
                    await ctx.ts.flush()
                    candidates.append(
                        self._contact_candidate(person, acc, fit, source="discovery", is_new=True)
                    )
                    new_count += 1
                seen_accounts.add(acc.id)

        return {"target": "contacts", "counts": {"own": len(top), "new": new_count},
                "candidates": candidates}

    @staticmethod
    def _contact_candidate(contact: Contact, account: Account, fit, *, source: str,
                           is_new: bool) -> dict:
        return {
            "entity": "contact", "id": contact.id, "name": contact.full_name,
            "title": contact.title, "domain": account.domain, "fit_score": fit.score,
            "fit_reasons": fit.reasons, "source": source, "is_new": is_new,
            "custom_fields": contact.custom_fields or {},
        }

    @staticmethod
    def _candidate(account: Account, fit, *, source: str, is_new: bool) -> dict:
        return {
            "entity": "account",
            "id": account.id,
            "name": account.name,
            "domain": account.domain,
            "industry": account.industry,
            "employee_count": account.employee_count,
            "country": account.country,
            "fit_score": fit.score,
            "fit_reasons": fit.reasons,
            "source": source,
            "is_new": is_new,
            "custom_fields": account.custom_fields or {},
        }


register_agent(DiscoveryAgent())
