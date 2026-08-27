# tests/test_token_budget.py
"""Keeping a completion inside the provider's token limits.

Measured against the live Groq account on 2026-08-26, because the failure was being read as a
context-window problem and it is not:

    17,000 chars -> 413  "Request too large ... on tokens per minute (TPM):
                          Limit 8000, Requested 8588, please reduce your message size"

Two further measurements shaped the design, and both contradict the obvious reading:

* **The five keys are five different organizations**, each with its own 8,000 TPM — verified by
  reading four distinct org ids out of four concurrent 429s. Rotation genuinely helps.
* **One large prompt nearly fills a whole org's minute.** Four concurrent ~7.5k-token requests on
  four different keys all 429'd with `Retry-After` of 28-33 seconds. That is why rotating without
  waiting fails: it burns every key in seconds, raises, and the caller falls through to the stub —
  whose output is emailed to real prospects.
"""
from __future__ import annotations

import pytest

from nexus.agents.llm import LLMMessage


# ---- fitting ---------------------------------------------------------------------------------

def test_a_prompt_within_budget_is_untouched():
    """Trimming a prompt that fits would cost quality for nothing."""
    from nexus.agents.token_budget import fit_messages

    msgs = [LLMMessage(role="user", content="short")]
    out, trimmed = fit_messages(msgs, 1000)
    assert trimmed is False
    assert out[0].content == "short"


def test_an_oversized_prompt_is_brought_under_the_budget():
    from nexus.agents.token_budget import estimate_tokens, fit_messages

    msgs = [LLMMessage(role="user", content="x" * 60_000)]
    out, trimmed = fit_messages(msgs, 2000)
    assert trimmed is True
    assert sum(estimate_tokens(m.content) for m in out) <= 2000


def test_the_system_message_is_never_trimmed():
    """It carries the output contract — the `Subject:` line the parser needs, the ban on inventing
    customer names and metrics. Losing the middle of a research blob costs detail; losing the
    contract produces confidently-wrong copy that goes to a real buyer."""
    from nexus.agents.token_budget import fit_messages

    system = "SYSTEM CONTRACT: always begin with Subject:. Never invent a customer or a metric."
    msgs = [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content="y" * 80_000),
    ]
    out, trimmed = fit_messages(msgs, 1500)
    assert trimmed is True
    assert out[0].content == system, "the output contract was trimmed away"


def test_trimming_keeps_the_head_and_the_tail():
    """The beginning states what the thing is; the end usually carries the most recent facts.
    Truncating to a prefix throws the second half of that away."""
    from nexus.agents.token_budget import fit_messages

    body = "HEAD-MARKER " + ("m" * 50_000) + " TAIL-MARKER"
    out, _ = fit_messages([LLMMessage(role="user", content=body)], 2000)
    assert "HEAD-MARKER" in out[0].content
    assert "TAIL-MARKER" in out[0].content
    assert "trimmed to fit" in out[0].content, "the reader must see that something was removed"


def test_the_last_message_keeps_the_most_room():
    """The most recent turn is the one being answered."""
    from nexus.agents.token_budget import estimate_tokens, fit_messages

    old = LLMMessage(role="user", content="o" * 40_000)
    recent = LLMMessage(role="user", content="r" * 40_000)
    out, _ = fit_messages([old, recent], 2000)
    assert estimate_tokens(out[1].content) >= estimate_tokens(out[0].content)


def test_the_estimate_errs_high():
    """Under-estimating tokens is what sends an over-sized request, which is the whole failure."""
    from nexus.agents.token_budget import estimate_tokens

    text = "The quick brown fox jumps over the lazy dog. " * 100
    # Real English is ~4 chars/token; the estimate must not be more generous than that.
    assert estimate_tokens(text) >= len(text) / 4


# ---- reading the provider's own limit ---------------------------------------------------------

def test_the_stated_limit_is_read_out_of_the_error():
    """Preferred over our constant: the account tier can move under us, which is exactly how this
    bug arrived — a model swap put the deployment on a tier whose ceiling nobody had encoded."""
    from nexus.agents.token_budget import parse_limit

    real = ("Request too large for model `openai/gpt-oss-120b` in organization `org_01j8` service "
            "tier `on_demand` on tokens per minute (TPM): Limit 8000, Requested 8588, please "
            "reduce your message size and try again.")
    assert parse_limit(real) == 8000
    assert parse_limit("something else entirely") is None


# ---- the provider's retry behaviour ------------------------------------------------------------

def test_retry_after_is_read_from_the_header():
    """Groq returns 28-33 seconds on an exhausted minute. The old code ignored it entirely and
    rotated with no wait at all."""
    from nexus.agents.llm import _retry_after_seconds

    class Resp:
        headers = {"retry-after": "29"}

    assert _retry_after_seconds(Resp()) == 29.0


def test_an_unparseable_retry_after_does_not_become_a_busy_loop():
    from nexus.agents.llm import _retry_after_seconds

    class Resp:
        headers = {"retry-after": "soon"}

    assert _retry_after_seconds(Resp()) >= 1.0


async def test_a_413_shrinks_the_prompt_and_succeeds(monkeypatch):
    """The measured failure, end to end. 413 means one request exceeded the whole minute budget:
    rotation and waiting are both useless, only a smaller prompt works."""
    import httpx

    from nexus.agents.llm import GroqLLMProvider

    sent: list[int] = []

    class Resp:
        def __init__(self, status, size=0):
            self.status_code = status
            self.headers = {}
            self._size = size

        def json(self):
            if self.status_code == 413:
                return {"error": {"message": (
                    "Request too large for model `openai/gpt-oss-120b` in organization `org_x` "
                    "service tier `on_demand` on tokens per minute (TPM): Limit 8000, "
                    "Requested 8588, please reduce your message size and try again."
                )}}
            return {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 42}}

        def raise_for_status(self):
            return None

    class Client:
        async def post(self, url, json=None, headers=None):
            size = sum(len(m["content"]) for m in json["messages"])
            sent.append(size)
            # Too big the first time, accepted once trimmed.
            return Resp(413) if len(sent) == 1 else Resp(200)

    p = GroqLLMProvider(api_keys=["k1"], model="openai/gpt-oss-120b")
    monkeypatch.setattr(p, "_http", lambda: Client())
    async def _noop():
        return None

    monkeypatch.setattr(p, "_refresh_keys", _noop)

    out = await p.complete([LLMMessage(role="user", content="z" * 60_000)])
    assert out.text == "ok"
    assert len(sent) == 2, "expected exactly one shrink-and-retry"
    assert sent[1] < sent[0], "the retry must be smaller, or the 413 repeats forever"


async def test_a_429_rotates_before_it_waits(monkeypatch):
    """The keys are separate organizations with separate budgets, so the next one may be clear.
    Waiting first would burn 30 seconds on a request another key would have served immediately."""
    import httpx

    from nexus.agents.llm import GroqLLMProvider

    used: list[str] = []

    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"retry-after": "30"}

        def json(self):
            if self.status_code == 429:
                return {"error": {"message": "rate limit"}}
            return {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 5}}

        def raise_for_status(self):
            return None

    class Client:
        async def post(self, url, json=None, headers=None):
            key = headers["Authorization"].split()[-1]
            used.append(key)
            return Resp(429) if key == "k1" else Resp(200)

    slept: list[float] = []

    async def no_sleep(sec):
        slept.append(sec)

    p = GroqLLMProvider(api_keys=["k1", "k2"], model="openai/gpt-oss-120b")
    monkeypatch.setattr(p, "_http", lambda: Client())
    async def _noop():
        return None

    monkeypatch.setattr(p, "_refresh_keys", _noop)
    monkeypatch.setattr("asyncio.sleep", no_sleep)

    out = await p.complete([LLMMessage(role="user", content="hi")])
    assert out.text == "ok"
    assert used == ["k1", "k2"], "it must try the second organization"
    assert slept == [], "it must not wait while an unexhausted key remains"


# ---- the concurrency gate -----------------------------------------------------------------------

async def test_the_gate_bounds_completions_process_wide():
    """The token budget belongs to the account and is shared by every endpoint. In the reported
    failure `/enrich`, `/lookalikes` and `/source-contacts` fired within the same second, and a
    per-caller limiter would have permitted exactly that."""
    import asyncio

    from nexus.agents.token_budget import CompletionGate

    gate = CompletionGate(limit=2)
    live = 0
    peak = 0

    async def work():
        nonlocal live, peak
        async with gate():
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1

    await asyncio.gather(*[work() for _ in range(10)])
    assert peak <= 2, f"{peak} completions ran at once against a limit of 2"


def test_the_gate_can_be_resized():
    """The right limit depends on the account tier, which is runtime-configurable."""
    from nexus.agents.token_budget import CompletionGate

    gate = CompletionGate(limit=2)
    gate.resize(5)
    assert gate._limit == 5
    gate.resize(0)
    assert gate._limit == 1, "a zero limit would deadlock every completion"


# ---- the pool-shrink trap -----------------------------------------------------------------------

async def test_adding_one_managed_key_warns_that_it_replaced_the_env_pool(client, monkeypatch):
    """Measured 2026-08-26, and the symptom looked nothing like the cause.

    The resolver falls back to the environment pool only when a provider has NO managed key, so
    adding one takes over completely. Adding a single Groq key through the panel replaced a five-key
    environment pool — and because each Groq key is a separate organisation with its own 8,000 TPM,
    usable throughput dropped from ~40,000 to 8,000 in one click. The visible symptom was 429s on
    `/enrich` and `/lookalikes`.

    The override is behaving as documented. What was missing was anyone being told.
    """
    from nexus.core.config import get_settings
    from tests.conftest import auth, signup

    email = "boss@pool1.com"
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    token = await signup(client, slug="pool1", email=email, company="POOL1")

    # A five-key environment pool, as the deployment had.
    monkeypatch.setattr(get_settings(), "groq_api_keys", "e1,e2,e3,e4,e5")

    clean = (await client.get("/api/admin/provider-keys/pool-health",
                              headers=auth(token))).json()
    assert clean == [], "nothing managed yet, so nothing has been replaced"

    await client.post("/api/admin/provider-keys", headers=auth(token),
                      json={"provider": "groq", "label": "only", "key": "gsk-one-key-11111"})

    warned = (await client.get("/api/admin/provider-keys/pool-health",
                               headers=auth(token))).json()
    row = next(r for r in warned if r["provider"] == "groq")
    assert row["managed_keys"] == 1
    assert row["environment_keys"] == 5
    assert "replace the environment pool" in row["detail"]


async def test_no_warning_once_every_key_is_managed(client, monkeypatch):
    """The warning is about losing keys, not about using the panel. Firing it whenever managed keys
    exist would train people to ignore it."""
    from nexus.core.config import get_settings
    from tests.conftest import auth, signup

    email = "boss@pool2.com"
    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    token = await signup(client, slug="pool2", email=email, company="POOL2")
    monkeypatch.setattr(get_settings(), "groq_api_keys", "e1,e2")

    for n in (1, 2, 3):
        await client.post("/api/admin/provider-keys", headers=auth(token),
                          json={"provider": "groq", "label": f"k{n}", "key": f"gsk-key-{n}0000"})

    health = (await client.get("/api/admin/provider-keys/pool-health",
                               headers=auth(token))).json()
    assert not any(r["provider"] == "groq" for r in health)
