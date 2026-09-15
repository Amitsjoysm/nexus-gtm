# nexus/verification/health.py
"""Can the email verifier actually answer? One check, used by Platform health, startup and the panel.

When Reacher cannot be reached nothing fails. The composite verifier falls back to DNS, which grades
every address on a mail-receiving domain `risky`, or to nothing, which grades it `unknown`. On screen
that is indistinguishable from a list of genuinely doubtful addresses, and staging ran that way for
days (found 2026-09-15) with nothing saying why.

The probe POSTs an EMPTY JSON body, so no mailbox is probed: the check never spends the verifier
host's SMTP reputation or looks like traffic to somebody's mail server.

**What proves Reacher answered is the shape of the reply, not its status.** Measured 2026-09-15
against the live instance: it defaults the missing `to_email` to "" and answers HTTP 200 with a full
verdict (`"is_reachable": "invalid"`, failed syntax, no MX lookup). Other versions reject the body
with 400 or 422. So a 200 carrying `is_reachable` is Reacher, and a 200 without it is something else
on that URL (a login page, a catch-all proxy). The first version of this check read every 200 as
"not Reacher", which would have flagged the working verifier as degraded.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

OK, DEGRADED, UNCONFIGURED, ERROR = "ok", "degraded", "unconfigured", "error"

#: A health check has to answer while an operator waits, even when the verifier's own per-address
#: timeout is generous.
CHECK_TIMEOUT_CAP_S = 10.0

#: Startup checks run in the background. The event loop only holds a weak reference to a task, so
#: one nobody keeps can be garbage-collected mid-run and its warning never logged.
_background: set[asyncio.Task] = set()


@dataclass(frozen=True, slots=True)
class VerifierCheck:
    status: str      # ok | degraded | unconfigured | error, the Platform health vocabulary
    detail: str      # never contains the Authorization header
    provider: str    # the email_verify_provider value that was checked
    url: str = ""    # empty when no Reacher key is configured


def _keys(provider: str) -> list[str]:
    return [k.strip().lower() for k in (provider or "").split(",") if k.strip()]


def _consequence(keys: list[str]) -> str:
    """What a rep sees while the verifier cannot answer, which is what makes the row actionable."""
    if "dns" in keys or "mx" in keys:
        return "every check falls back to DNS, which grades every address on a mail domain risky"
    return "every check reads unknown"


def _looks_like_reacher(resp: httpx.Response) -> bool:
    try:
        body = resp.json()
    except Exception:
        return False
    return isinstance(body, dict) and "is_reachable" in body


def _describe(resp: httpx.Response, keys: list[str]) -> tuple[str, str]:
    code = resp.status_code
    if code in (400, 422):
        return OK, f"Reacher answered (HTTP {code} to an empty request, as expected)"
    if code == 200:
        if _looks_like_reacher(resp):
            return OK, "Reacher answered (a verdict for an empty address, no mailbox probed)"
        return DEGRADED, (
            "answered HTTP 200 without a Reacher verdict, so this does not look like Reacher's "
            "/v0/check_email"
        )
    then = _consequence(keys)
    if code in (401, 403):
        return ERROR, (
            f"refused (HTTP {code}): the endpoint wants an Authorization header it did not get; "
            f"{then}"
        )
    if code == 404:
        return ERROR, (
            f"not a Reacher endpoint (HTTP 404): check the path ends /v0/check_email; {then}"
        )
    if code == 405:
        return ERROR, (
            "answers but not with POST (HTTP 405): a proxy is in front that does not forward to "
            f"Reacher; {then}"
        )
    if code >= 500:
        return ERROR, f"a proxy answered but Reacher behind it did not (HTTP {code}); {then}"
    return ERROR, f"unexpected HTTP {code}; {then}"


async def check_email_verifier(
    *, transport: httpx.AsyncBaseTransport | None = None,
) -> VerifierCheck:
    """What the verifier in force would do right now, overrides included. Never raises.

    ``transport`` is a test seam (``httpx.MockTransport``); None means the real network.
    """
    provider = ""
    try:
        from nexus.core.config import get_settings

        settings = get_settings()
        provider = settings.email_verify_provider or ""
        keys = _keys(provider)

        if "reacher" not in keys:
            if "dns" in keys or "mx" in keys:
                return VerifierCheck(DEGRADED, (
                    "DNS only: domains are checked, no mailbox is ever confirmed, so no address "
                    "can read valid. Add reacher to the email verification setting."
                ), provider)
            return VerifierCheck(UNCONFIGURED, (
                f"{provider or 'stub'}: syntax check only, every address reads unknown. Set email "
                "verification to reacher,dns."
            ), provider)

        url = (settings.email_verify_url or "").strip()
        if not url:
            return VerifierCheck(
                ERROR, f"reacher is selected but no verifier URL is set; {_consequence(keys)}",
                provider,
            )

        timeout = min(float(settings.email_verify_timeout_s or CHECK_TIMEOUT_CAP_S),
                      CHECK_TIMEOUT_CAP_S)
        auth = settings.email_verify_auth_header or ""
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)), transport=transport,
            ) as client:
                resp = await client.post(
                    url, json={}, headers={"Authorization": auth} if auth else None,
                )
            status, detail = _describe(resp, keys)
        except Exception as exc:
            # The exception type only. Its message is not needed to act on this, and a verbatim
            # network error is the oracle `nexus/alerts/url_guard.py` exists to close.
            status, detail = ERROR, f"unreachable ({type(exc).__name__}): {_consequence(keys)}"

        if urlparse(url).scheme == "http":
            detail += ". Plain http: addresses cross the network in cleartext"
        return VerifierCheck(status, detail, provider, url)
    except Exception as exc:
        return VerifierCheck(ERROR, f"could not run the check ({type(exc).__name__})", provider)


async def warn_if_verifier_unreachable(
    logger: logging.Logger | None = None,
    *, transport: httpx.AsyncBaseTransport | None = None,
) -> VerifierCheck | None:
    """Log the check where an operator reading a boot log will see it. Never raises."""
    log = logger or logging.getLogger("nexus.verification.health")
    try:
        check = await check_email_verifier(transport=transport)
    except Exception:
        log.warning("could not check the email verifier", exc_info=True)
        return None
    if check.status == ERROR:
        log.warning("EMAIL VERIFIER UNREACHABLE: %s (provider=%s url=%s)",
                    check.detail, check.provider, check.url)
    elif check.status == DEGRADED:
        log.warning("email verifier degraded: %s (provider=%s)", check.detail, check.provider)
    else:
        log.info("email verifier %s: %s", check.status, check.detail)
    return check


def schedule_verifier_warning(logger: logging.Logger | None = None) -> asyncio.Task:
    """Run the startup check in the background: a slow or dead verifier must never delay boot."""
    task = asyncio.get_running_loop().create_task(
        warn_if_verifier_unreachable(logger), name="nexus-email-verifier-check",
    )
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task
