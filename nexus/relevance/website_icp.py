"""AI-drafted ICP from a company's own website.

A founder/SDR pastes their website URL; this gathers what the company does (web search) and asks
the LLM to infer the ideal customer profile of the businesses THEY SELL TO — industries, company
size, geography, required tech, buyer titles, value props, and product context. The result is a
DRAFT the user reviews and edits in the Relevance engine before saving; nothing is persisted here.

Offline (stub search/LLM) yields an empty draft — never fabricated. Never raises across the boundary.
"""
from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger("nexus.relevance.website_icp")

_JSON_OBJ = re.compile(r"\{.*\}", re.DOTALL)

# Models wrap JSON in ```json ... ``` fences even when told not to — measured against Claude on a
# live deployment, with "Output ONLY a JSON object — no prose, no code fences" in the system
# prompt. The greedy `\{.*\}` above survives a fence, but only when the object CLOSES; strip them
# first so a truncated-inside-a-fence response fails for the one reason that is actually true.
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def domain_of(url: str) -> str:
    """Best-effort registrable host from a pasted URL/domain. '' when unparseable."""
    u = (url or "").strip()
    if u and "://" not in u:
        u = "https://" + u
    parsed = urlparse(u)
    host = (parsed.netloc or parsed.path).lower().strip("/")
    if host.startswith("www."):
        host = host[4:]
    return host.split("/")[0]


def _empty_draft() -> dict:
    return {"icp": {}, "value_props": [], "product_context": ""}


def _strlist(x, cap: int = 20) -> list[str]:
    return [str(v).strip() for v in (x or []) if str(v).strip()][:cap]


def _intn(x) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _coerce(data: dict) -> dict:
    """Normalize the LLM's JSON into the RelevanceProfileIn shape, dropping anything malformed."""
    icp_in = data.get("icp") or {}
    icp = {
        "industries": _strlist(icp_in.get("industries")),
        "countries": _strlist(icp_in.get("countries")),
        "required_tech": _strlist(icp_in.get("required_tech")),
        "employee_min": _intn(icp_in.get("employee_min")),
        "employee_max": _intn(icp_in.get("employee_max")),
        "buyer_titles": _strlist(icp_in.get("buyer_titles"), cap=10),
    }
    value_props = []
    for vp in (data.get("value_props") or [])[:6]:
        if isinstance(vp, dict) and str(vp.get("name", "")).strip():
            value_props.append({
                "name": str(vp["name"]).strip()[:120],
                "description": str(vp.get("description", "")).strip()[:400],
                "pains_solved": _strlist(vp.get("pains_solved"), cap=8),
            })
    return {
        "icp": icp,
        "value_props": value_props,
        "product_context": str(data.get("product_context", "")).strip()[:1000],
    }


async def analyze_website_to_icp(url: str, *, search, llm) -> dict:
    """Draft an ICP from a website URL. Returns ``{icp, value_props, product_context}`` (empty on
    failure or offline). ``search`` is the registry search callable; ``llm`` an LLM provider."""
    domain = domain_of(url)
    if not domain:
        return _empty_draft()

    # 1) Gather what the company does from public web results.
    snippets: list[str] = []
    for query in (
        f"{domain} product what they do customers",
        f"{domain} pricing case studies industries served",
    ):
        try:
            hits = await search(query, limit=5) or []
        except Exception:  # a flaky search must not break the draft
            hits = []
        for h in hits:
            title = getattr(h, "title", "") or ""
            snippet = getattr(h, "snippet", "") or ""
            link = getattr(h, "url", "") or ""
            snippets.append(f"- {title} :: {snippet} ({link})")
    blob = "\n".join(snippets[:12]).strip()
    if not blob:
        # The THIRD way this returns an empty draft, and the only one that never reaches the LLM —
        # so none of the logging below can report it. A dead or unkeyed search provider produces
        # exactly the same blank form and the same "couldn't analyze that site" toast as a genuinely
        # uninformative website, which is how a provider outage reads as a product defect.
        logger.warning(
            "website_icp: no web results for %s, so the ICP draft is empty and no LLM call was "
            "made. Check the configured search provider has a working key.",
            domain,
        )
        return _empty_draft()

    # 2) LLM infers the ICP of who they SELL TO.
    from nexus.agents.llm import LLMMessage

    system = (
        "You are a GTM strategist. From a company's web presence, infer the ideal customer "
        "profile (ICP) of the businesses THEY SELL TO — not the company itself. "
        "Output ONLY a JSON object — no prose, no code fences."
    )
    user = (
        f"Company domain: {domain}\nWeb research:\n{blob}\n\n"
        'Return a JSON object with keys: "icp": {"industries": [..], "countries": [..], '
        '"required_tech": [..], "employee_min": int|null, "employee_max": int|null, '
        '"buyer_titles": [up to 8 job titles]}, "value_props": [{"name","description",'
        '"pains_solved":[..]}], "product_context": "1-3 sentences on what the company does and '
        'its differentiators". Base every field on the research; if unknown use [] or null.'
    )
    try:
        resp = await llm.complete(
            [LLMMessage("system", system), LLMMessage("user", user)],
            # 800 was NOT enough and the failure was invisible. Measured against Claude on a live
            # deployment: the requested object — industries, countries, tech, 8 buyer titles, 6
            # value props each with a description and a pains list, plus product context — ran to
            # ~2,996 characters (~750 tokens) and stopped AT the cap, mid-string.
            #
            # A truncated object has no closing brace, so `_JSON_OBJ` matches nothing and the
            # function returns an empty draft. The user sees "Couldn't analyze that site" after a
            # perfectly successful, fully billed LLM call.
            temperature=0.1, max_tokens=2000, purpose="website_icp",
        )
    except Exception as exc:  # provider isolation
        logger.warning("website_icp llm failed for %s: %r", domain, exc)
        return _empty_draft()

    # EVERY FAILURE BELOW LOGS. They used to return an empty draft silently, which is
    # indistinguishable from "this site has no discoverable ICP" — so a truncated response, a
    # fenced response and a genuinely uninformative website all produced the same blank form and
    # the same unhelpful toast. The response length is included because it is what identifies
    # truncation: a value at or just under the token cap is the tell.
    raw = _FENCE.sub("", (resp.text or "").strip())
    match = _JSON_OBJ.search(raw)
    if not match:
        logger.warning(
            "website_icp: no JSON object in LLM response for %s (len=%d, likely truncated at "
            "max_tokens or prose-only). head=%r",
            domain, len(raw), raw[:200],
        )
        return _empty_draft()
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "website_icp: LLM response for %s was not valid JSON (len=%d): %s. tail=%r",
            domain, len(raw), exc, raw[-200:],
        )
        return _empty_draft()
    if not isinstance(data, dict):
        logger.warning("website_icp: LLM returned %s, not an object, for %s", type(data).__name__, domain)
        return _empty_draft()

    draft = _coerce(data)
    if not draft["icp"].get("industries") and not draft["product_context"]:
        # Parsed cleanly but every field the UI checks is empty — almost always a key-name
        # mismatch (the model answering with `geographies`/`roles`/`company_size` instead of the
        # requested `countries`/`buyer_titles`/`employee_min`). Worth seeing the keys it DID send.
        logger.warning(
            "website_icp: parsed JSON for %s but coerced to an empty draft. "
            "top-level keys=%s icp keys=%s",
            domain, sorted(data.keys()), sorted((data.get("icp") or {}).keys()),
        )
    return draft
