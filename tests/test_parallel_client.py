"""The Parallel client: Entity Search and Decisions, total and frugal (2026-10-29).

Shapes are the documented ones (docs.parallel.ai, read 2026-10-28/29): Entity Search answers
``{entity_set_id, entities: [{name, url, description}]}``; Decisions answers
``{answers: {name: {type, ...}}, citations: [{url, title, excerpts}], decision_id}``, where a
``refusal`` is a successful answer, not an error. The key is the ``x-api-key`` header.
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.integrations import parallel as pl

ENTITIES = {"entity_set_id": "entity_set_1", "entities": [
    {"name": "EOG Resources", "url": "https://www.eogresources.com",
     "description": "Independent crude oil and natural gas company headquartered in Houston"},
    {"name": "Coterra Energy", "url": "https://www.coterra.com", "description": "E&P company"},
]}
DECISION = {"mode": "fast", "decision_id": "decisions_1", "answers": {
    "industry": {"type": "choice", "choice": "oil_gas",
                 "probabilities": {"oil_gas": 0.93, "other": 0.05, "unknown": 0.02}},
    "hq_country": {"type": "choice", "choice": "united_states",
                   "probabilities": {"united_states": 0.97, "other": 0.02, "unknown": 0.01}},
    "size_band": {"type": "refusal"},
}, "citations": [{"url": "https://www.eogresources.com/about", "title": "About EOG",
                  "excerpts": ["EOG Resources is one of the largest crude oil and natural gas"]}]}


class Recorder:
    """A transport that answers from a script and records what was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.sent: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        status, body = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return httpx.Response(status, json=body)

    def transport(self):
        return httpx.MockTransport(self)


def _client(recorder, keys=("key-one",)):
    return pl.ParallelClient(keys=list(keys), transport=recorder.transport(), backoff_s=0)


async def test_no_key_sends_nothing_and_says_so():
    rec = Recorder((200, ENTITIES))
    outcome = await pl.ParallelClient(keys=[], transport=rec.transport()).entity_search(
        "companies", "Oil and gas companies in the United States")
    assert outcome.status == "not_configured" and not rec.sent


async def test_entity_search_sends_the_documented_request_and_reads_the_entities():
    rec = Recorder((200, ENTITIES))
    outcome = await _client(rec).entity_search(
        "companies", "Oil and gas companies in the United States", match_limit=100)

    assert outcome.status == "ok" and not outcome.cached
    assert [e.name for e in outcome.entities] == ["EOG Resources", "Coterra Energy"]
    request = rec.sent[0]
    assert request.url == "https://api.parallel.ai/v1beta/findall/entity-search"
    assert request.headers["x-api-key"] == "key-one" and "key-one" not in str(request.url)
    assert json.loads(request.content) == {
        "entity_type": "companies", "objective": "Oil and gas companies in the United States",
        "match_limit": 100}


async def test_the_same_search_is_bought_once_and_a_failure_is_never_cached():
    failing = Recorder((500, {"error": {"message": "boom"}}))
    first = await _client(failing).entity_search("companies", "Same objective")
    assert first.status == "unavailable" and len(failing.sent) == 2, "one retry, then give up"

    rec = Recorder((200, ENTITIES))
    second = await _client(rec).entity_search("companies", "Same objective")
    third = await _client(rec).entity_search("companies", "Same objective")
    assert second.status == third.status == "ok"
    assert len(rec.sent) == 1, "the failure was not cached, the answer was"
    assert third.cached and [e.name for e in third.entities] == ["EOG Resources", "Coterra Energy"]


async def test_a_rejected_key_gives_way_to_the_next():
    rec = Recorder((401, {"error": {"message": "Invalid API key"}}), (200, ENTITIES))
    outcome = await _client(rec, keys=("bad-key", "good-key")).entity_search("people", "Ops leads")
    assert outcome.status == "ok"
    assert [r.headers["x-api-key"] for r in rec.sent] == ["bad-key", "good-key"]


async def test_out_of_credit_is_its_own_answer_and_names_no_key():
    rec = Recorder((402, {"type": "error", "error": {
        "ref_id": "x", "message": "Payment required: insufficient credit in account"}}))
    outcome = await _client(rec, keys=("secret-key",)).entity_search("companies", "Anything")
    assert outcome.status == "out_of_credit"
    assert "insufficient credit" in outcome.detail and "secret-key" not in outcome.detail


async def test_busy_is_retried_once_then_reported():
    rec = Recorder((429, {"error": {"message": "Too many requests"}}))
    outcome = await _client(rec).entity_search("companies", "Busy objective")
    assert outcome.status == "busy" and len(rec.sent) == 2


async def test_a_match_limit_outside_the_documented_range_is_refused_before_sending():
    rec = Recorder((200, ENTITIES))
    outcome = await _client(rec).entity_search("companies", "Too many", match_limit=5000)
    assert outcome.status == "invalid" and not rec.sent


async def test_decisions_reads_choices_refusals_and_citations():
    rec = Recorder((200, DECISION))
    outcome = await _client(rec).decide(
        {"company": "EOG Resources", "website": "https://www.eogresources.com"},
        {"industry": {"type": "choice", "instructions": "x",
                      "criteria": {"oil_gas": "a", "other": "b", "unknown": "c"}}},
        cache_subject="eogresources.com|q1")

    assert outcome.status == "ok"
    assert outcome.answers["industry"].choice == "oil_gas"
    assert outcome.answers["industry"].probabilities["oil_gas"] == pytest.approx(0.93)
    assert outcome.answers["size_band"].type == "refusal" and outcome.answers["size_band"].choice is None
    assert outcome.citations[0]["url"] == "https://www.eogresources.com/about"
    request = rec.sent[0]
    assert request.url == "https://api.parallel.ai/v1/decisions"
    body = json.loads(request.content)
    assert body["state"]["company"] == "EOG Resources" and "industry" in body["questions"]


async def test_a_decision_is_bought_once_per_subject():
    rec = Recorder((200, DECISION))
    question = {"industry": {"type": "choice", "instructions": "x",
                             "criteria": {"oil_gas": "a", "unknown": "c"}}}
    await _client(rec).decide({"company": "EOG"}, question, cache_subject="eog.com|qs")
    again = await _client(rec).decide({"company": "EOG"}, question, cache_subject="eog.com|qs")
    assert again.cached and len(rec.sent) == 1


async def test_the_key_probe_is_free_and_reads_404_as_authenticated():
    from nexus.providers import testing

    good = Recorder((404, {"detail": "Not found"}))
    result = await testing.probe("parallel", "key-one", transport=good.transport())
    assert result.ok and result.status == "probe_ok"
    assert good.sent[0].method == "GET", "a GET is neither billed nor rate limited"

    bad = Recorder((401, {"error": {"message": "Invalid API key"}}))
    result = await testing.probe("parallel", "nope", transport=bad.transport())
    assert not result.ok and result.http_status == 401


async def test_verify_makes_one_smallest_real_search():
    from nexus.providers import testing

    rec = Recorder((200, ENTITIES))
    result = await testing.verify("parallel", "key-one", transport=rec.transport())
    assert result.ok and result.status == "verified"
    assert json.loads(rec.sent[0].content)["match_limit"] == 5


async def test_an_unreadable_answer_is_not_an_empty_one():
    rec = Recorder((200, {"unexpected": True}))
    outcome = await _client(rec).entity_search("companies", "Odd answer")
    assert outcome.status == "bad_response"
