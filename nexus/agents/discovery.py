# nexus/agents/discovery.py
"""Discovery Agent — surface companies (or contacts) matching a conversation's ICP.

For companies it does two things: ranks the tenant's own accounts by deterministic ICP fit, as
context, and adds up to the same number of NEW companies through the chain Populate and the daily
sweep use (`prospecting/companies.find_icp_companies` → `prospecting/deliver.deliver`), persisted as
``source="discovery"`` and charged ``discovery.account_added`` like them. The two used to share one
cap, own first, so a workspace holding 25 accounts never got a new company (reported 2026-10-01).
The conversation's ICP — not the saved RelevanceProfile — drives the scoring, so discovery reflects
exactly what the rep asked for.
"""
from __future__ import annotations

from nexus.agents.runtime import AgentContext, BaseAgent, register_agent
from nexus.core.config import get_settings
from nexus.models.account import Account, Contact
from nexus.models.relevance import RelevanceProfile


#: What a company the orchestrator adds is charged as: the same capability as Populate and the daily
#: sweep, because it is the same result (decided with the product owner 2026-10-01).
ADD_CAPABILITY = "discovery.account_added"


def _linkedin_client():
    """The LinkedIn actor client; ``None`` is the configured Apify pool. A seam for tests."""
    return None


def _web_search(ctx):
    """The web company search the chain falls back to: the runtime's registry."""
    registry = getattr(ctx, "registry", None)
    return registry.company_search if registry is not None else None


def _refusal(exc, need: int) -> str:
    """The sentence a refused balance check becomes in the run's notes."""
    detail = getattr(exc, "reason", "") or str(exc)
    return (f"No new companies were added: adding {need} needs more credits than this workspace "
            f"has ({detail}). Top up or lower the number, then run it again.")


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
        # The LinkedIn codes the ICP's industries map to, so a LinkedIn-sourced account labelled
        # "Software Development" counts as an industry match for "SaaS" (as on the saved ICP).
        if scoring_icp.get("industries"):
            from nexus.prospecting.industries import map_industries

            scoring_icp["linkedin_industry_ids"] = await map_industries(scoring_icp["industries"])
            profile.icp = scoring_icp

        accounts = await ctx.ts.list(Account)
        survivors = [a for a in accounts if _passes_hard_filters(a, scoring_icp)]
        scored = []
        for a in survivors:
            fit = ctx.relevance.score_icp_fit(profile, a)
            scored.append((fit, a))
        scored.sort(key=lambda t: (-t[0].score, t[1].name))
        own = scored[:max_candidates]
        candidates = [self._candidate(a, fit, source="own", is_new=False) for (fit, a) in own]

        # NEW companies are asked for whatever the workspace already holds. They used to be asked
        # for only when fewer than `max_candidates` own accounts existed, so a workspace past 25
        # accounts was shown 25 of its own and never got a new one (reported 2026-10-01).
        new_accounts, notes, discarded = await self._add_new_companies(
            ctx, profile, scoring_icp, max_candidates)
        for acc in new_accounts:
            fit = ctx.relevance.score_icp_fit(profile, acc)
            candidates.append(self._candidate(acc, fit, source="discovery", is_new=True))

        return {
            "target": "companies",
            "counts": {"own": len(own), "new": len(new_accounts), "requested": max_candidates},
            "notes": notes,
            "discarded": discarded,
            "candidates": candidates,
        }

    async def _add_new_companies(self, ctx: AgentContext, profile, scoring_icp: dict,
                                 need: int) -> tuple[list[Account], dict, dict]:
        """Up to ``need`` companies the workspace does not hold, through the chain Populate and the
        daily sweep use, at their price. Returns ``(accounts, notes, discarded)``. Never raises:
        a source, the balance or the charge failing costs the new companies, never the own list.

        * The balance is checked for the whole request BEFORE anything is searched, as Populate
          does: a search nobody can pay for is spend with nothing to show for it.
        * Web candidates must clear ``icp_discovery_min_fit`` ("high relevance"); LinkedIn and
          database candidates are ICP matches by construction (`prospecting/deliver.py`).
        * The accounts and their ``discovery.account_added`` charge share a SAVEPOINT, so a charge
          refused at the last moment takes the accounts with it rather than adding them free.
        """
        from collections import Counter

        from nexus.billing.errors import QuotaExceeded
        from nexus.billing.meter import metered
        from nexus.prospecting.companies import find_icp_companies
        from nexus.prospecting.deliver import deliver

        settings = get_settings()
        owner = ctx.inputs.get("owner_user_id")
        run_id = ctx.inputs.get("run_id")
        if need <= 0:
            return [], {}, {}
        try:
            from nexus.billing.entitlements import preflight

            (await preflight(ctx.ts, ADD_CAPABILITY, quantity=need)).raise_if_blocked()
        except QuotaExceeded as exc:
            return [], {"billing": _refusal(exc, need)}, {}
        except Exception:  # noqa: BLE001 - a billing read that fails must not stop discovery
            pass

        chain = await find_icp_companies(
            ctx.ts, scoring_icp, need, client=_linkedin_client(), web_search=_web_search(ctx),
            web_pool=need * 3, linkedin_budget_s=settings.discovery_linkedin_budget_s)
        discarded = Counter(chain.discarded)
        notes = dict(chain.notes)
        try:
            async with ctx.ts.session.begin_nested():
                ids, _ = await deliver(ctx.ts, profile, chain.candidates, limit=need,
                                       source="discovery", min_fit=settings.icp_discovery_min_fit,
                                       discarded=discarded, owner_user_id=owner)
                if ids:
                    async with metered(ctx.ts, ADD_CAPABILITY, quantity=len(ids), user_id=owner,
                                       source="api",
                                       idempotency_key=f"discover:{run_id}" if run_id else None,
                                       attrs={"run_id": run_id, "via": "orchestrator"}):
                        pass
        except QuotaExceeded as exc:
            return [], {**notes, "billing": _refusal(exc, need)}, dict(discarded)
        added: list[Account] = []
        for account_id in ids:
            account = await ctx.ts.get(Account, account_id)
            if account is not None:
                added.append(account)
        return added, notes, dict(discarded)

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
