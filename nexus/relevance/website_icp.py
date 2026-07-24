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
            temperature=0.1, max_tokens=800, purpose="website_icp",
        )
    except Exception as exc:  # provider isolation
        logger.warning("website_icp llm failed for %s: %r", domain, exc)
        return _empty_draft()

    match = _JSON_OBJ.search(resp.text or "")
    if not match:
        return _empty_draft()
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return _empty_draft()
    return _coerce(data) if isinstance(data, dict) else _empty_draft()
