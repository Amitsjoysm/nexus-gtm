# tests/test_verifier_health.py
"""Whether the email verifier can actually answer, said before a rep finds out.

Staging graded every found address `unknown` or `risky` for days. The composite verifier falls back
to DNS when Reacher cannot be reached, and DNS grades every address on a mail-receiving domain
`risky`, so "Reacher is down" and "these addresses are doubtful" looked identical and nothing said
which it was.

The check posts an EMPTY body. Reacher rejects that with a 4xx, which proves it is there, and no
mailbox is probed: a health check must not spend the verifier host's SMTP reputation.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging

import httpx
import pytest

from nexus.core.config import get_settings
from nexus.verification import health

URL = "https://verifier.example.com/v0/check_email"


def _configure(monkeypatch, *, provider="reacher,dns", url=URL, auth=""):
    s = get_settings()
    monkeypatch.setattr(s, "email_verify_provider", provider)
    monkeypatch.setattr(s, "email_verify_url", url)
    monkeypatch.setattr(s, "email_verify_auth_header", auth)
    monkeypatch.setattr(s, "email_verify_timeout_s", 20.0)


def _answering(code: int, seen: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(code, json={"message": "missing field `to_email`"})

    return httpx.MockTransport(handler)


def _failing(exc: Exception) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return httpx.MockTransport(handler)


def _no_network() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"no request expected, got {request.url}")

    return httpx.MockTransport(handler)


# ---- what each configuration reads as -----------------------------------------------------------

async def test_the_stub_is_unconfigured_and_costs_no_request(monkeypatch):
    _configure(monkeypatch, provider="stub")
    check = await health.check_email_verifier(transport=_no_network())
    assert check.status == "unconfigured"
    assert "unknown" in check.detail
    assert check.url == ""


async def test_dns_alone_is_degraded_because_nothing_can_read_valid(monkeypatch):
    _configure(monkeypatch, provider="dns")
    check = await health.check_email_verifier(transport=_no_network())
    assert check.status == "degraded"
    assert "no mailbox" in check.detail


@pytest.mark.parametrize(("code", "status", "says"), [
    (400, "ok", "answered"),
    (422, "ok", "answered"),
    (200, "degraded", "does not look like Reacher"),
    (401, "error", "Authorization"),
    (403, "error", "Authorization"),
    (404, "error", "/v0/check_email"),
    (405, "error", "proxy"),
    (502, "error", "Reacher behind it"),
    (418, "error", "unexpected HTTP 418"),
])
async def test_each_answer_says_what_it_means(monkeypatch, code, status, says):
    _configure(monkeypatch)
    check = await health.check_email_verifier(transport=_answering(code))
    assert check.status == status, check.detail
    assert says in check.detail
    assert check.url == URL


async def test_the_live_reacher_answering_200_with_a_verdict_is_ok(monkeypatch):
    """Measured 2026-09-15 on 158.69.113.104:8080: this Reacher defaults the missing `to_email` to ""
    and answers 200 with a full verdict. The first version of the check read every 200 as "not
    Reacher" and flagged the working verifier. The shape is the proof, not the status."""
    measured = {
        "input": "", "is_reachable": "invalid",
        "misc": {"is_disposable": False, "is_role_account": False},
        "mx": {"accepts_mail": False, "records": []},
        "smtp": {"can_connect_smtp": False, "is_catch_all": False, "is_deliverable": False},
        "syntax": {"address": None, "domain": "", "is_valid_syntax": False, "username": ""},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=measured)

    _configure(monkeypatch)
    check = await health.check_email_verifier(transport=httpx.MockTransport(handler))
    assert check.status == "ok", check.detail
    assert "Reacher answered" in check.detail


async def test_a_200_that_is_not_json_is_not_mistaken_for_reacher(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Sign in</html>")

    _configure(monkeypatch)
    check = await health.check_email_verifier(transport=httpx.MockTransport(handler))
    assert check.status == "degraded"


async def test_an_unreachable_verifier_names_the_consequence(monkeypatch):
    """"Unreachable" alone sends nobody anywhere. What an operator needs is why every address on
    their screen is amber."""
    _configure(monkeypatch, provider="reacher,dns")
    chained = await health.check_email_verifier(transport=_failing(httpx.ConnectError("refused")))
    assert chained.status == "error"
    assert "unreachable" in chained.detail
    assert "risky" in chained.detail

    _configure(monkeypatch, provider="reacher")
    alone = await health.check_email_verifier(transport=_failing(httpx.ReadTimeout("slow")))
    assert alone.status == "error"
    assert "unknown" in alone.detail


async def test_no_address_is_ever_sent(monkeypatch):
    _configure(monkeypatch)
    seen: list[httpx.Request] = []
    await health.check_email_verifier(transport=_answering(400, seen))
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert json.loads(seen[0].content or b"{}") == {}


async def test_the_auth_header_is_sent_and_never_echoed(monkeypatch):
    _configure(monkeypatch, auth="Bearer s3cret-token")
    seen: list[httpx.Request] = []
    check = await health.check_email_verifier(transport=_answering(401, seen))
    assert seen[0].headers.get("authorization") == "Bearer s3cret-token"
    assert "s3cret" not in check.detail


async def test_plain_http_is_flagged_without_failing_the_check(monkeypatch):
    _configure(monkeypatch, url="http://158.69.113.104:8080/v0/check_email")
    check = await health.check_email_verifier(transport=_answering(400))
    assert check.status == "ok"
    assert "cleartext" in check.detail


async def test_the_check_never_raises(monkeypatch):
    _configure(monkeypatch)
    check = await health.check_email_verifier(transport=_failing(RuntimeError("boom")))
    assert check.status == "error"


# ---- at startup ---------------------------------------------------------------------------------

async def test_an_unreachable_verifier_is_logged_loudly(monkeypatch, caplog):
    _configure(monkeypatch)
    log = logging.getLogger("nexus.test.verifier")
    with caplog.at_level(logging.INFO, logger="nexus.test.verifier"):
        await health.warn_if_verifier_unreachable(
            log, transport=_failing(httpx.ConnectError("no route"))
        )
    assert any(
        r.levelno == logging.WARNING and "EMAIL VERIFIER UNREACHABLE" in r.getMessage()
        for r in caplog.records
    )


async def test_the_startup_warning_never_raises(monkeypatch):
    """A health check that takes the process down at boot is worse than no health check."""
    async def boom(**_):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(health, "check_email_verifier", boom)
    assert await health.warn_if_verifier_unreachable(logging.getLogger("nexus.test")) is None


async def test_scheduling_the_warning_does_not_wait_for_it(monkeypatch):
    gate = asyncio.Event()

    async def slow(logger=None, **_):
        await gate.wait()

    monkeypatch.setattr(health, "warn_if_verifier_unreachable", slow)
    task = health.schedule_verifier_warning(logging.getLogger("nexus.test"))
    assert not task.done()
    assert task in health._background, "an unreferenced task can be garbage-collected mid-run"
    gate.set()
    await task
    await asyncio.sleep(0)
    assert task not in health._background


def test_the_api_and_the_worker_both_warn_at_startup():
    from nexus import main
    from nexus.workers import worker

    assert "schedule_verifier_warning" in inspect.getsource(main)
    boot = inspect.getsource(worker._main)
    # The worker checks what is IN FORCE, so it applies overrides first. Checking the environment
    # URL while an override points elsewhere would warn about the wrong host.
    assert "refresh_if_stale(force=True)" in boot
    assert boot.index("refresh_if_stale(force=True)") < boot.index("schedule_verifier_warning")
