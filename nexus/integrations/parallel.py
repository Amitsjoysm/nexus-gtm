"""Parallel's Entity Search and Decisions APIs. Total: never raises; every failure is a status.

Read from docs.parallel.ai on 2026-10-28/29 (`docs/superpowers/specs/2026-10-29-parallel-provider-
design.md`). Two calls, both synchronous and priced per request, which is what makes them frugal:

* **Entity Search** (``POST /v1beta/findall/entity-search``): people or companies for a
  natural-language objective, ranked but NOT verified, ``match_limit`` 5-1,000. $0.005 a request
  with 100 results included, so a caller asks for 100 and keeps what survives its own gates.
* **Decisions** (``POST /v1/decisions``): up to 100 typed questions about ONE entity over one set
  of retrieved evidence. $0.001 a request. A ``refusal`` is a successful answer, never an error.

Rules this module keeps:

* **Keys** come from Provider keys (``parallel``) through the resolver, falling back to
  ``NEXUS_PARALLEL_API_KEYS``. A 401/403 key is recorded against its row and the next is tried.
* **The key is the ``x-api-key`` header**, never part of a URL, and never echoed in a detail.
* **The host is configuration** (``parallel_base_url``), never a request parameter.
* **402 is ``out_of_credit``**: Parallel's own prepaid balance is empty. It is not a bad key and not
  a busy service, and saying which is what lets an operator fix it.
* **A 429 or 5xx is retried once**, after a short backoff, then reported.
* **Answers are cached in ``web_cache``**, never failures: a search for 24 h by objective, a
  decision for 90 days by the caller's subject. A cached answer reports ``cached`` so the caller
  records no cost for it.

State is per call, never on the instance.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Literal

import httpx

logger = logging.getLogger("nexus.integrations.parallel")

PROVIDER = "parallel"
ENTITY_SEARCH_PATH = "/v1beta/findall/entity-search"
DECISIONS_PATH = "/v1/decisions"
#: Entity Search's documented range.
MIN_MATCH_LIMIT, MAX_MATCH_LIMIT = 5, 1000
#: Results one Entity Search request includes in its price.
INCLUDED_RESULTS = 100
MAX_QUESTIONS = 100
ENTITY_CACHE_TTL_S = 24 * 3600
DECISION_CACHE_TTL_S = 90 * 24 * 3600
DEFAULT_TIMEOUT = httpx.Timeout(45.0, connect=5.0)

Status = Literal["ok", "not_configured", "invalid", "rejected", "out_of_credit", "busy",
                 "unavailable", "bad_response"]


@dataclass(slots=True)
class Entity:
    name: str
    url: str
    description: str = ""


@dataclass(slots=True)
class EntityOutcome:
    status: Status
    entities: list[Entity] = field(default_factory=list)
    detail: str = ""
    #: Served from the cache: nothing was bought, so no cost is recorded.
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(slots=True)
class Answer:
    type: str                          # choice | noul | score | refusal
    choice: str | None = None
    probabilities: dict = field(default_factory=dict)
    noul: float | None = None


@dataclass(slots=True)
class DecisionOutcome:
    status: Status
    answers: dict[str, Answer] = field(default_factory=dict)
    citations: list[dict] = field(default_factory=list)
    detail: str = ""
    cached: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _words(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text or ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
        detail = body.get("detail")
        if isinstance(detail, list) and detail:
            return "; ".join(str(d.get("msg", d)) for d in detail[:3] if isinstance(d, dict))
        if detail:
            return str(detail)
    return str(body)


def _clean(text: str, keys: list[str]) -> str:
    text = " ".join(str(text or "").split())
    for key in keys:
        if key:
            text = text.replace(key, "•••")
    return text[:300]


def parse_entities(body) -> list[Entity] | None:
    if not isinstance(body, dict) or not isinstance(body.get("entities"), list):
        return None
    out = []
    for item in body["entities"]:
        if isinstance(item, dict) and item.get("name"):
            out.append(Entity(name=str(item["name"]).strip(), url=str(item.get("url") or ""),
                              description=str(item.get("description") or "").strip()))
    return out


def parse_answers(body) -> tuple[dict[str, Answer], list[dict]] | None:
    if not isinstance(body, dict) or not isinstance(body.get("answers"), dict):
        return None
    answers: dict[str, Answer] = {}
    for name, raw in body["answers"].items():
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "")
        if kind == "choice":
            answers[name] = Answer("choice", choice=str(raw.get("choice") or "") or None,
                                   probabilities=dict(raw.get("probabilities") or {}))
        elif kind == "noul":
            value = raw.get("noul")
            answers[name] = Answer("noul", noul=float(value) if value is not None else None)
        elif kind == "score":
            answers[name] = Answer("score", probabilities=dict(raw.get("probabilities") or {}))
        else:
            answers[name] = Answer("refusal")
    citations = [c for c in body.get("citations") or [] if isinstance(c, dict) and c.get("url")]
    return answers, citations


class ParallelClient:
    def __init__(self, *, keys: list[str] | None = None, base_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 timeout: httpx.Timeout = DEFAULT_TIMEOUT, backoff_s: float = 1.0) -> None:
        self._keys = keys
        self._base_url = base_url
        self._transport = transport
        self._timeout = timeout
        self._backoff_s = backoff_s

    async def _resolve_keys(self) -> list[str]:
        if self._keys is not None:
            return [k for k in self._keys if k]
        from nexus.providers import resolver

        try:
            return [k for k in await resolver.key_pool(PROVIDER) if k]
        except Exception:
            logger.warning("could not resolve %s keys", PROVIDER, exc_info=True)
            return []

    def _url(self, path: str) -> str:
        from nexus.core.config import get_settings

        base = (self._base_url or getattr(get_settings(), "parallel_base_url", "")
                or "https://api.parallel.ai")
        return base.rstrip("/") + path

    # ---- the two calls -------------------------------------------------------------------------

    async def entity_search(self, entity_type: str, objective: str, *,
                            match_limit: int = INCLUDED_RESULTS) -> EntityOutcome:
        try:
            return await self._entity_search(entity_type, objective, match_limit)
        except Exception:
            logger.warning("parallel entity search failed unexpectedly", exc_info=True)
            return EntityOutcome("unavailable", detail="Parallel could not be reached.")

    async def decide(self, state: dict, questions: dict, *,
                     cache_subject: str | None = None) -> DecisionOutcome:
        try:
            return await self._decide(state, questions, cache_subject)
        except Exception:
            logger.warning("parallel decision failed unexpectedly", exc_info=True)
            return DecisionOutcome("unavailable", detail="Parallel could not be reached.")

    async def _entity_search(self, entity_type: str, objective: str,
                             match_limit: int) -> EntityOutcome:
        from nexus.fetching import cache

        objective = " ".join(str(objective or "").split())
        if entity_type not in ("companies", "people") or not objective:
            return EntityOutcome("invalid", detail="An entity type and an objective are needed.")
        if not MIN_MATCH_LIMIT <= int(match_limit) <= MAX_MATCH_LIMIT:
            return EntityOutcome("invalid", detail="match_limit must be between 5 and 1000.")
        subject = f"{entity_type}|{objective}"
        hit = await cache.get("parallel_es", subject, limit=match_limit)
        if isinstance(hit, list):
            return EntityOutcome("ok", entities=[Entity(**e) for e in hit if isinstance(e, dict)],
                                 cached=True)
        status, body, detail = await self._post(
            ENTITY_SEARCH_PATH, {"entity_type": entity_type, "objective": objective,
                                 "match_limit": int(match_limit)})
        if status != "ok":
            return EntityOutcome(status, detail=detail)
        entities = parse_entities(body)
        if entities is None:
            return EntityOutcome("bad_response", detail="Parallel sent an answer we could not read.")
        await cache.put("parallel_es", subject, limit=match_limit, ttl_s=ENTITY_CACHE_TTL_S,
                        payload=[{"name": e.name, "url": e.url, "description": e.description}
                                 for e in entities])
        return EntityOutcome("ok", entities=entities)

    async def _decide(self, state: dict, questions: dict,
                      cache_subject: str | None) -> DecisionOutcome:
        from nexus.fetching import cache

        if not questions or len(questions) > MAX_QUESTIONS:
            return DecisionOutcome("invalid", detail="Between 1 and 100 questions are needed.")
        if cache_subject:
            hit = await cache.get("parallel_dec", cache_subject)
            parsed = parse_answers(hit) if isinstance(hit, dict) else None
            if parsed is not None:
                return DecisionOutcome("ok", answers=parsed[0], citations=parsed[1], cached=True)
        status, body, detail = await self._post(DECISIONS_PATH,
                                                {"state": state, "questions": questions})
        if status != "ok":
            return DecisionOutcome(status, detail=detail)
        parsed = parse_answers(body)
        if parsed is None:
            return DecisionOutcome("bad_response", detail="Parallel sent an answer we could not read.")
        if cache_subject:
            await cache.put("parallel_dec", cache_subject, ttl_s=DECISION_CACHE_TTL_S,
                            payload={"answers": body["answers"],
                                     "citations": [{"url": c["url"], "title": c.get("title")}
                                                   for c in parsed[1]]})
        return DecisionOutcome("ok", answers=parsed[0], citations=parsed[1])

    # ---- one request, with rotation and one retry ---------------------------------------------

    async def _post(self, path: str, body: dict) -> tuple[Status, object, str]:
        keys = await self._resolve_keys()
        if not keys:
            return ("not_configured", None,
                    "Parallel is not set up: a superadmin adds a Parallel key in "
                    "Control plane > Provider keys.")
        url = self._url(path)
        async with httpx.AsyncClient(timeout=self._timeout, transport=self._transport) as client:
            last: tuple[Status, object, str] = ("unavailable", None, "Parallel could not be reached.")
            for attempt in range(2):
                retry = False
                for key in keys:
                    try:
                        resp = await client.post(url, headers={"x-api-key": key}, json=body)
                    except httpx.HTTPError as exc:
                        logger.warning("parallel transport error: %s", type(exc).__name__)
                        last, retry = ("unavailable", None, "Parallel could not be reached."), True
                        break
                    code = resp.status_code
                    if code == 200:
                        try:
                            return "ok", resp.json(), ""
                        except ValueError:
                            return "bad_response", None, "Parallel sent an answer we could not read."
                    if code in (401, 403):
                        await self._record_rejection(key, resp)
                        last = ("rejected", None, "Parallel rejected our key. A superadmin can "
                                "check it in Control plane > Provider keys.")
                        continue
                    if code == 402:
                        return ("out_of_credit", None,
                                "Parallel's balance is empty: "
                                + (_clean(_words(resp), keys) or "payment required") + ".")
                    if code in (400, 404, 408, 422):
                        return "invalid", None, _clean(_words(resp), keys) or "Parallel refused it."
                    if code == 429:
                        last, retry = ("busy", None, "Parallel is busy. Try again shortly."), True
                        continue
                    last, retry = ("unavailable", None,
                                   f"Parallel failed (HTTP {code}). Try again shortly."), True
                    break
                if retry and attempt == 0:
                    if self._backoff_s > 0:
                        await asyncio.sleep(self._backoff_s)
                    continue
                break
        return last

    @staticmethod
    async def _record_rejection(key: str, resp: httpx.Response) -> None:
        from nexus.providers.service import record_rejection_from_response

        try:
            await record_rejection_from_response(PROVIDER, key, resp)
        except Exception:
            logger.warning("could not record a rejected %s key", PROVIDER, exc_info=True)
