# tests/test_apify_client.py
"""The Apify seam: key rotation, inert-until-keyed, and no fake successes.

Apify is where the lookups with no compliant public API live (a phone behind a LinkedIn profile, a
Crunchbase page). The rules here are the ones the search-provider seam learned the expensive way:
an unkeyed integration must raise rather than return an empty list, a revoked key must not take the
whole pool down, and rotation state must be shared so a fresh caller does not restart on an already
rate-limited key.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.integrations.apify import ACTORS, ApifyClient, ApifyError, ApifyNotConfigured


class _Transport:
    """Scripted responses, recording the token used for each call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.tokens: list[str] = []
        self.urls: list[str] = []
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        # Read from the AUTHORIZATION HEADER, not the query string. The client used to send
        # `?token=<key>`, which httpx copies into `HTTPStatusError` — one 400 printed a live API
        # key into the exception message and the logs. The rotation behaviour these tests pin is
        # unchanged; only where the credential travels is.
        auth = request.headers.get("authorization", "")
        self.tokens.append(auth[len("Bearer "):] if auth.startswith("Bearer ") else "")
        assert "token=" not in str(request.url), (
            f"the Apify key is in the URL again: {request.url}"
        )
        self.urls.append(str(request.url.path))
        import json as _json

        self.bodies.append(_json.loads(request.content or b"{}"))
        status, payload = self.responses.pop(0) if self.responses else (200, [])
        return httpx.Response(status, json=payload)


def _patch(monkeypatch, transport: _Transport):
    """Route the client's httpx through a scripted transport."""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def test_an_unkeyed_client_raises_rather_than_returning_nothing():
    """An empty list and a missing key look identical to a caller. This codebase has already
    shipped one source that silently found nothing forever."""
    with pytest.raises(ApifyNotConfigured):
        await ApifyClient([]).run_actor("phone_finder", {})


async def test_a_logical_actor_name_resolves_to_its_id(monkeypatch):
    t = _Transport((200, [{"phone": "+14155552671"}]))
    _patch(monkeypatch, t)
    items = await ApifyClient(["k1"]).run_actor("phone_finder", {"linkedin_url": ["u"]})

    assert items == [{"phone": "+14155552671"}]
    assert ACTORS["phone_finder"] in t.urls[0]
    assert t.bodies[0] == {"linkedin_url": ["u"]}


async def test_a_raw_actor_id_is_passed_through(monkeypatch):
    """So trying a new actor does not require editing the registry first."""
    t = _Transport((200, []))
    _patch(monkeypatch, t)
    await ApifyClient(["k1"]).run_actor("someRawActorId", {})
    assert "someRawActorId" in t.urls[0]


async def test_a_rate_limited_key_rotates_to_the_next(monkeypatch):
    t = _Transport((429, {}), (200, [{"ok": True}]))
    _patch(monkeypatch, t)
    client = ApifyClient(["k1", "k2"])
    items = await client.run_actor("phone_finder", {})

    assert items == [{"ok": True}]
    assert t.tokens == ["k1", "k2"], "the second attempt must use the second key"


async def test_rotation_is_sticky_across_calls(monkeypatch):
    """A fresh client per call would restart at key 0 and hammer an already-limited key, which is
    why the module keeps one process-wide instance."""
    t = _Transport((429, {}), (200, [{"a": 1}]), (200, [{"b": 2}]))
    _patch(monkeypatch, t)
    client = ApifyClient(["k1", "k2"])
    await client.run_actor("phone_finder", {})
    await client.run_actor("phone_finder", {})

    assert t.tokens == ["k1", "k2", "k2"], "must stay on the working key, not reset to k1"


async def test_a_revoked_key_is_skipped_not_retried(monkeypatch):
    """One dead key in a pool must not take the integration down, and must not be retried either."""
    t = _Transport((401, {}), (200, [{"ok": True}]))
    _patch(monkeypatch, t)
    client = ApifyClient(["dead", "live"])
    assert await client.run_actor("phone_finder", {}) == [{"ok": True}]
    assert t.tokens == ["dead", "live"]


async def test_a_single_revoked_key_raises(monkeypatch):
    """With nothing to rotate to, this is a configuration error and must surface as one."""
    t = _Transport((401, {}))
    _patch(monkeypatch, t)
    with pytest.raises(ApifyError):
        await ApifyClient(["dead"]).run_actor("phone_finder", {})


async def test_a_failing_actor_raises_rather_than_reporting_no_results(monkeypatch):
    t = _Transport((500, {}), (500, {}), (500, {}), (500, {}), (500, {}))
    _patch(monkeypatch, t)
    with pytest.raises(ApifyError):
        await ApifyClient(["k1"]).run_actor("phone_finder", {})


async def test_non_dict_dataset_rows_are_dropped(monkeypatch):
    """Actors are third-party code; a stray string in the dataset must not crash the caller."""
    t = _Transport((200, [{"good": 1}, "junk", None, {"also_good": 2}]))
    _patch(monkeypatch, t)
    items = await ApifyClient(["k1"]).run_actor("phone_finder", {})
    assert items == [{"good": 1}, {"also_good": 2}]


# ---- a registered actor must be a wired actor -------------------------------------------------
#
# `crunchbase_org` and `company_search` sat in ACTORS for months with no caller. That state is
# worse than either alternative: the actor verification script printed a row for each of them, so
# they read as capabilities in the one place an operator looks, while doing nothing. They were
# removed 2026-08-20. These two tests keep the registry honest structurally, the way
# `test_nothing_reads_company_signals_yet` does for the shadow crawl — by scanning the tree, so it
# stays true by test rather than by memory.


def _actor_names_passed_to_run_actor() -> set[str]:
    """Every actor name that product code actually hands to ``run_actor``.

    Resolved through the AST rather than by grepping the name, because both cheap approximations
    are wrong here and in opposite directions. Searching for ``run_actor("<name>"`` misses
    `linkedin_profile`, which is passed via the module constant `apify_provider.ACTOR`. Searching
    for the bare quoted name falsely matches `company_search`, which is also an unrelated module
    (`integrations/company_search.py`), a registry method, and a cache key literal
    (``_norm_key("company_search", ...)``) — so that spelling would have reported a dead actor as
    wired. Reading the argument is the only version that answers the question asked.
    """
    import ast
    from pathlib import Path

    invoked: set[str] = set()
    for path in Path("nexus").rglob("*.py"):
        if path.name == "apify.py":
            continue                       # the registry may name its own actors
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Module-level `ACTOR = "linkedin_profile"` indirection has to resolve, or a caller gets
        # reported as missing for the crime of naming a constant.
        consts = {
            t.id: n.value.value
            for n in tree.body
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
            for t in n.targets
            if isinstance(t, ast.Name)
        }
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "run_actor":
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                invoked.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in consts:
                invoked.add(consts[arg.id])
    return invoked


def test_every_registered_actor_has_a_real_caller():
    """ACTORS is a list of capabilities. An entry nothing calls is a claim the product cannot back.

    Finds the call site rather than trusting a hand-maintained list, because the failure being
    prevented is exactly a hand-maintained list drifting out of date.
    """
    unwired = sorted(set(ACTORS) - _actor_names_passed_to_run_actor())
    assert unwired == [], (
        f"registered but called by nothing: {unwired}. Wire it to a consumer or drop it from "
        f"ACTORS — a registered actor with no caller looks like a capability and is not one."
    )


def test_the_verification_script_declares_a_consumer_for_every_actor():
    """`verify_apify_actors.py` prints a consumer per actor. An actor missing from that map used to
    print the literal string 'none', which is a report that the tool is working as intended rather
    than the alarm it should be."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from verify_apify_actors import CONSUMERS

    assert set(CONSUMERS) == set(ACTORS), (
        "the verify script's consumer map and ACTORS have drifted: "
        f"only in ACTORS={set(ACTORS) - set(CONSUMERS)}, "
        f"only in CONSUMERS={set(CONSUMERS) - set(ACTORS)}"
    )


async def test_the_key_pool_reads_primary_then_rotation_list():
    from nexus.core.config import get_settings

    s = get_settings()
    object.__setattr__(s, "apify_api_key", "primary")
    object.__setattr__(s, "apify_api_keys", "primary, second ,third")
    try:
        assert s.apify_api_key_list == ["primary", "second", "third"], "deduped, primary first"
    finally:
        object.__setattr__(s, "apify_api_key", "")
        object.__setattr__(s, "apify_api_keys", "")


# ---- what an auth failure actually says ------------------------------------------------------
#
# Measured against the live API 2026-08-05: the phone_finder actor (code_crafter/mobile-finder)
# 403s with `full-permission-actor-not-approved` until someone approves its permissions in the
# Apify console. Both keys were valid and both could see the actor — so "key rejected", and worse
# the pool-exhaustion message that followed, sent the reader to rotate credentials and then to
# wait for a rate limit that did not exist. The provider's own reason has to survive.

async def test_a_permission_403_reports_the_real_reason_not_a_rate_limit(monkeypatch):
    import httpx

    from nexus.integrations.apify import ApifyClient, ApifyError

    body = {
        "error": {
            "type": "full-permission-actor-not-approved",
            "message": "This Actor requires full access to your account.",
        }
    }

    class _Resp:
        status_code = 403
        text = str(body)

        def json(self):
            return body

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient())
    client = ApifyClient(api_keys=["k1", "k2"])

    with pytest.raises(ApifyError) as exc:
        await client.run_actor("phone_finder", {"linkedin_url": ["x"]})

    message = str(exc.value)
    assert "full-permission-actor-not-approved" in message
    # The specific regression: a permanent auth problem must not be reported as a transient one.
    assert "rate limit" not in message.lower()


async def test_a_genuine_rate_limit_still_reports_as_one(monkeypatch):
    """The fix must not swing the other way — a real 429 pool exhaustion still says so."""
    import httpx

    from nexus.integrations.apify import ApifyClient, ApifyError

    class _Resp:
        status_code = 429
        text = ""

        def json(self):
            return {}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _FakeClient())
    monkeypatch.setattr("nexus.integrations.apify._RETRY_BACKOFFS", (0, 0))
    client = ApifyClient(api_keys=["k1", "k2"])

    with pytest.raises(ApifyError) as exc:
        await client.run_actor("phone_finder", {"linkedin_url": ["x"]})
    assert "rate limits" in str(exc.value).lower()


# ---- the credential must not ride in the URL -----------------------------------------------------

def test_the_token_is_sent_as_a_header_never_a_query_parameter():
    """A live key leak, observed 2026-09-08 against a real Apify token.

    The client passed `params={"token": key}`. Apify accepts that, and httpx puts the FULL URL into
    `HTTPStatusError` — so one 400 from a wrong actor input printed the entire live API key into the
    exception message, the log line and anything that reports them:

        Client error '400 Bad Request' for url
        'https://api.apify.com/v2/acts/.../run-sync-get-dataset-items?token=apify_api_...'

    This file's own discipline is that a key is never rendered — `key_hint` is all the panel gets.
    That has to hold for request plumbing too, or the panel refusing to show the key is decoration.
    """
    import inspect

    from nexus.integrations import apify

    src = inspect.getsource(apify)
    assert 'params={"token"' not in src, "the Apify token is back in the query string"
    assert "Authorization" in inspect.getsource(apify.ApifyClient.run_actor)


def test_no_provider_test_puts_a_token_in_a_url():
    """The same leak in the panel's own Test button, which is the one place an operator is most
    likely to be looking at an error message when it fails."""
    import pathlib

    src = pathlib.Path("nexus/providers/testing.py").read_text(encoding="utf-8")
    assert "token={key}" not in src, "a provider probe still interpolates the key into its URL"


# ---- an actor can refuse inside a 200 -------------------------------------------------------------

def test_an_actor_refusal_row_raises_instead_of_reading_as_no_results():
    """THE bug this found. Measured 2026-09-08 against `dev_fusion/Linkedin-Profile-Scraper`: the
    run succeeded at the HTTP layer and the dataset was one row reading "Users on the free Apify
    plan can run the actor through the UI and not via other methods."

    Every check in `run_actor` passed, so it returned that row, `parse_profile` found no headline
    and no posts, and `refresh_person_insights` returned None. A hard refusal presented to the
    operator as "this person has no social footprint" — for every contact, forever.

    That is this integration's recurring shape: a real problem wearing "no results". `phone_finder`
    extracting nothing because of key spellings, `build_personalization_provider` returning the stub
    for a configured provider, and now this.
    """
    import pytest

    from nexus.integrations.apify import ApifyError, _raise_if_actor_refused

    with pytest.raises(ApifyError) as exc:
        _raise_if_actor_refused("act1", [{"error": "Users on the free Apify plan can run the "
                                                   "actor through the UI and not via other methods."}])
    assert "free Apify plan" in str(exc.value)


def test_a_refusal_carrying_apifys_own_error_type_still_raises():
    from nexus.integrations.apify import ApifyError, _raise_if_actor_refused
    import pytest

    with pytest.raises(ApifyError):
        _raise_if_actor_refused("act1", [{"error": "nope", "errorType": "x", "statusCode": 403}])


def test_a_row_that_carries_real_data_beside_an_error_field_is_data():
    """The opposite bug, and the reason this is not a blanket "any row with an error" check. An
    actor that reports a partial failure alongside a usable result must not have that result thrown
    away — dropping it would invent the mirror image of the bug above."""
    from nexus.integrations.apify import _raise_if_actor_refused

    _raise_if_actor_refused("act1", [{"error": "partial", "headline": "VP Sales",
                                      "posts": ["a real post"]}])


def test_a_normal_dataset_is_untouched():
    """Every shape that is NOT a bare refusal: real rows, several rows, an empty dataset, a blank
    error string. A guard that fired on any of these would take down working actors."""
    from nexus.integrations.apify import _raise_if_actor_refused

    _raise_if_actor_refused("act1", [])
    _raise_if_actor_refused("act1", [{"headline": "VP Sales"}])
    _raise_if_actor_refused("act1", [{"error": "a"}, {"error": "b"}])
    _raise_if_actor_refused("act1", [{"error": "   "}])


def test_the_refusal_reaches_the_personalization_provider_as_a_failure():
    """`ApifyPersonalizationProvider.fetch` already logs and returns None on an exception, which is
    the right posture — a social-fetch outage must never break enrichment. What was missing was
    anything to catch: the refusal arrived as a successful empty answer, so the log line that names
    the real reason never ran."""
    import inspect

    from nexus.personalization import apify_provider

    src = inspect.getsource(apify_provider.ApifyPersonalizationProvider.fetch)
    assert "except Exception" in src and "logger.warning" in src
