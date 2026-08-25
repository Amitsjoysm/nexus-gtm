"""Twilio call provider — real dialling over the Twilio REST API.

Activated by ``NEXUS_TELEPHONY_PROVIDER=twilio``. Adding an Account SID, Auth Token, and
caller-ID number to the environment is the only step between a shipped build and a live call:
there is no fake mode here and no fallback to the click-to-dial stub.

Three properties carried over from ``nexus/integrations/apify.py``, for the same reasons:

* **Inert until keyed.** No credentials means a clear ``TelephonyNotConfigured``, never a quiet
  downgrade to click-to-dial. An operator who configured Twilio, watched ``tel:`` links keep
  working and concluded calls were going through it would never be corrected by anything in the
  system — the exact failure that made ``build_call_provider`` start raising.
* **The provider's own error text survives.** Twilio returns ``{"code": 21211, "message": ...}``,
  and the code is the actionable part: an unverified caller ID, a number Twilio cannot route,
  and a bad token all arrive as a 4xx and need entirely different fixes.
* **No new dependency.** The published snippet uses ``twilio``; this uses ``httpx``, already
  vendored. The REST calls are the same ones that library wraps.

**The bridge model.** Twilio's ``POST /Calls.json`` needs either a publicly reachable TwiML
``Url`` or an inline ``Twiml`` document. We use the latter and place a *rep-first bridge*:
Twilio rings the rep's own phone, and when they answer it dials the prospect and joins the two
legs. That is Twilio's own click-to-call pattern, it is the closest live analogue of the manual
``tel:`` workflow it replaces, and it needs no inbound webhook — so NEXUS never has to be
publicly reachable by Twilio to place a call.

**No retries on the dial.** ``run_actor`` in the Apify client retries 5xx and rotates on 429,
which is right for an idempotent scrape. ``POST /Calls.json`` is not idempotent: a retry after
an ambiguous failure can ring a prospect twice and bill twice. The read-only lookups below are
safe to repeat, so only they are best-effort; the dial fails once, loudly.
"""
from __future__ import annotations

import logging
import re

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

from nexus.calling.provider import (
    AgentNumberRequired,
    CallHandle,
    CallProvider,
    CallProviderError,
    InvalidPhoneNumber,
    TelephonyNotConfigured,
)

logger = logging.getLogger("nexus.calling.twilio")

# E.164: a leading '+', a non-zero country code, then up to 14 more digits.
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
# Everything a human might type around the digits of a phone number.
_PUNCT = re.compile(r"[\s().\-/]")


class TwilioSettings(BaseSettings):
    """Twilio credentials, read from ``NEXUS_TWILIO_*``.

    These live here rather than in :mod:`nexus.core.config` only to keep the credential block
    self-contained; the prefix and semantics are identical to the main ``Settings``, so folding
    them in later needs no caller change.
    """

    model_config = SettingsConfigDict(
        env_prefix="NEXUS_", env_file=".env", extra="ignore"
    )

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_api_base: str = "https://api.twilio.com/2010-04-01"
    twilio_timeout_s: float = 20.0
    # Recording is a legal decision (two-party-consent jurisdictions), never a default.
    twilio_record_calls: bool = False


def normalize_e164(number: str | None, *, field: str) -> str:
    """Strip human punctuation and require E.164, so a bad number fails with our message
    instead of an opaque Twilio 21211."""
    candidate = _PUNCT.sub("", (number or "").strip())
    if not _E164.match(candidate):
        raise InvalidPhoneNumber(
            f"{field} must be an E.164 phone number like +15551234567, got {number!r}"
        )
    return candidate


def _bridge_twiml(*, prospect: str, caller_id: str) -> str:
    """TwiML for the second leg: once the rep answers, dial the prospect and bridge them.

    Both numbers are already E.164-validated, so no character here can escape the document.
    """
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        f"<Response><Dial callerId=\"{caller_id}\"><Number>{prospect}</Number></Dial></Response>"
    )


class TwilioCallProvider(CallProvider):
    name = "twilio"

    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        api_base: str = "https://api.twilio.com/2010-04-01",
        timeout: float = 20.0,
        record_calls: bool = False,
        transport=None,
    ):
        if not (account_sid or "").strip() or not (auth_token or "").strip():
            # Plain ASCII: this message reaches Windows consoles and log shippers that are not
            # always UTF-8, and a startup error that itself fails to encode helps nobody.
            raise TelephonyNotConfigured(
                "twilio is not configured. Set NEXUS_TWILIO_ACCOUNT_SID and "
                "NEXUS_TWILIO_AUTH_TOKEN, or leave NEXUS_TELEPHONY_PROVIDER=stub for "
                "click-to-dial."
            )
        self.account_sid = account_sid.strip()
        # Never surfaced by __repr__, never logged, never returned by the API.
        self._auth_token = auth_token.strip()
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.record_calls = record_calls
        # Test seam (httpx.MockTransport); None = real network.
        self._transport = transport

    def __repr__(self) -> str:  # pragma: no cover - trivial, but keeps the token out of logs
        return f"TwilioCallProvider(account_sid={self.account_sid!r})"

    # ------------------------------------------------------------------ HTTP

    def _url(self, path: str) -> str:
        return f"{self.api_base}/Accounts/{self.account_sid}/{path.lstrip('/')}"

    async def _request(self, method: str, url: str, data: dict | None = None) -> dict:
        """One Twilio call. Raises :class:`CallProviderError` on anything but a 2xx."""
        # Cap the connect phase so an unreachable host fails in seconds rather than burning the
        # full read timeout while a rep waits on the dial button.
        timeout = httpx.Timeout(self.timeout, connect=min(5.0, self.timeout))
        try:
            async with httpx.AsyncClient(
                timeout=timeout, transport=self._transport
            ) as client:
                resp = await client.request(
                    method, url, data=data,
                    auth=(self.account_sid, self._auth_token),
                )
        except Exception as exc:  # transport, DNS, timeout
            raise CallProviderError(f"twilio request failed: {exc!r}") from None
        if resp.status_code >= 300:
            raise CallProviderError(_describe_error(resp))
        try:
            return resp.json()
        except Exception:
            raise CallProviderError("twilio returned a non-JSON response") from None

    async def _best_effort(self, method: str, url: str) -> dict | None:
        """Enrichment lookups never raise: a missing recording must not block the rep from
        logging what happened on the call."""
        try:
            return await self._request(method, url)
        except CallProviderError as exc:
            logger.warning("twilio lookup failed (%s): %s", url, exc)
            return None

    # ------------------------------------------------------------------ dialling

    async def place_call(
        self, *, to: str, from_: str, context: dict | None = None
    ) -> CallHandle:
        """Ring the rep, then bridge them to the prospect.

        ``context['agent_number']`` is the rep's phone — the first leg. ``to`` is the prospect
        and ``from_`` is the Twilio-owned caller ID shown to them.

        Unlike the email verifier, which degrades a failure to ``unknown``, this **raises**.
        Returning a manual ``tel:`` handle after a failed Twilio request would report a call as
        placed when no phone ever rang.
        """
        agent_number = (context or {}).get("agent_number")
        if not agent_number:
            raise AgentNumberRequired(
                "agent_number is required: the bridge rings the rep's phone first"
            )
        prospect = normalize_e164(to, field="to")
        caller_id = normalize_e164(from_, field="from_")
        agent = normalize_e164(agent_number, field="agent_number")

        form = {
            "To": agent,
            "From": caller_id,
            "Twiml": _bridge_twiml(prospect=prospect, caller_id=caller_id),
        }
        if self.record_calls:
            form["Record"] = "true"

        data = await self._request("POST", self._url("Calls.json"), data=form)
        call_sid = data.get("sid")
        if not call_sid:
            raise CallProviderError("twilio accepted the call but returned no sid")
        return CallHandle(mode="live", provider_call_id=call_sid)

    # ------------------------------------------------------------------ outcome enrichment

    async def get_call_status(self, provider_call_id: str) -> dict | None:
        """The call's real status and duration — what makes a logged outcome trustworthy
        without a public status webhook."""
        data = await self._best_effort("GET", self._url(f"Calls/{provider_call_id}.json"))
        if not data:
            return None
        return {"status": data.get("status"), "duration_s": _as_int(data.get("duration"))}

    async def _first_recording_sid(self, provider_call_id: str) -> str | None:
        data = await self._best_effort(
            "GET", self._url(f"Calls/{provider_call_id}/Recordings.json")
        )
        recordings = (data or {}).get("recordings") or []
        return recordings[0].get("sid") if recordings else None

    async def get_recording(self, provider_call_id: str) -> str | None:
        """The recording's media URL, or ``None`` when the call was not recorded.

        Twilio can take a few seconds after hangup to expose a recording, so ``None`` here is a
        normal outcome — the activity keeps a NULL rather than a fabricated URL.
        """
        rec_sid = await self._first_recording_sid(provider_call_id)
        if not rec_sid:
            return None
        return f"{self.api_base}/Accounts/{self.account_sid}/Recordings/{rec_sid}.mp3"

    async def get_transcript(self, provider_call_id: str) -> str | None:
        """Transcription text, or ``None``. Twilio transcription is opt-in per recording; when
        it is off there is no text, and none is invented."""
        rec_sid = await self._first_recording_sid(provider_call_id)
        if not rec_sid:
            return None
        data = await self._best_effort(
            "GET", self._url(f"Recordings/{rec_sid}/Transcriptions.json")
        )
        entries = (data or {}).get("transcriptions") or []
        for entry in entries:
            text = (entry.get("transcription_text") or "").strip()
            if text:
                return text
        return None


def _describe_error(resp: httpx.Response) -> str:
    """Twilio's own error text, so the reason survives into our exception.

    Twilio returns ``{"code": 21211, "message": ...}``. The code is the actionable part:
    21210 is an unverified caller ID, 21211 an unroutable ``To``, 20003 a rejected credential.
    All three are 4xx, and an operator told only "Twilio returned 400" cannot tell them apart.

    Only the documented ``code``/``message`` fields are read — the body is never echoed
    wholesale, so nothing that travelled with the request can come back out in a log line.
    """
    code = ""
    detail = ""
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            code = str(payload.get("code") or "").strip()
            detail = str(payload.get("message") or "").strip()
    except Exception:
        pass
    head = f"twilio returned {resp.status_code}"
    if code:
        head += f" (code {code})"
    return f"{head}: {detail}" if detail else head


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_twilio_provider() -> TwilioCallProvider:
    """Construct from ``NEXUS_TWILIO_*``. Raises when unkeyed — never falls back to the stub."""
    s = TwilioSettings()
    return TwilioCallProvider(
        account_sid=s.twilio_account_sid,
        auth_token=s.twilio_auth_token,
        api_base=s.twilio_api_base,
        timeout=s.twilio_timeout_s,
        record_calls=s.twilio_record_calls,
    )
