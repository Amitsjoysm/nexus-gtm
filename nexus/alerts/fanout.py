# nexus/alerts/fanout.py
"""Post an alert to the shared channels somebody asked for — once each.

**The bug this closes**, found on the live deployment 2026-09-10: `routing.route()` turns "send
funding alerts to Teams, straight away" into a delivery decision, and it had NO production caller.
Every signal alert was created with the default `channel="in_app"` and delivered only there — 315 of
315 on the live database — while a saved `funding -> teams, immediate` preference sat unread for six
days and the setup screen told the person "Alerts are ready". `route()` was tested thoroughly, as a
pure function, and nothing tested that the path ingestion actually runs consulted it.

**Who decides where an alert goes** — two sources, unioned:

* Workspace rules (`AlertChannelRule`): a manager says "this channel receives funding alerts". They
  post for every account and nobody's personal `off` or quiet hours mutes them — a rep silencing
  funding for themselves must not silence the team's Slack. They are also the only way an alert on
  an UNOWNED account reaches a channel.
* Personal routes (`NotificationPreference`): one member's choice, scoped to "all" of the category
  or only accounts they own ("mine"), and put through `route()` so their own off / digest / quiet
  hours apply to their own route and nobody else's.

**Once per channel.** Slack, Teams and Telegram are one per workspace, so three reps routing funding
to Slack is one post, not three. The union is a set, and that is the whole mechanism.

**It adds, never replaces.** The alert row still exists and is still delivered in-app by
`AlertService.create`; this only adds destinations. And it never raises: a channel being down costs
that channel's post, never the alert and never the next channel — the same rule as the rest of
alerting, where losing the signal to save the notification would be exactly backwards.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("nexus.alerts.fanout")

#: Channels a rule or route may post to. `in_app` is excluded because creating the alert already
#: delivered it there. `webhook` is excluded because it is the operator's deployment-level
#: integration, not a place a member routes their alerts.
EXTERNAL_CHANNELS: tuple[str, ...] = ("slack", "teams", "telegram", "email")


async def channels_for(
    ts, *, category: str, severity: str, owner_user_id: str | None, now
) -> list[str]:
    """Which external channels an alert of this category should reach, deduplicated."""
    from nexus.alerts.routing import minutes_utc, route
    from nexus.models.alerts import AlertChannelRule
    from nexus.models.notification_preference import NotificationPreference

    wanted: set[str] = {
        rule.channel
        for rule in await ts.list(AlertChannelRule, AlertChannelRule.category == category)
        if rule.channel in EXTERNAL_CHANNELS
    }

    utc_minutes = minutes_utc(now)
    prefs = await ts.list(
        NotificationPreference,
        NotificationPreference.category == category,
        NotificationPreference.channel.in_(EXTERNAL_CHANNELS),
    )
    for pref in prefs:
        if pref.channel in wanted:
            continue  # already going there — once per channel
        # "Mine" means an account this person owns. Unowned is nobody's, so it does not qualify.
        if (pref.scope or "all") == "mine" and pref.user_id != owner_user_id:
            continue
        decision = route(pref, severity=severity, utc_minutes=utc_minutes)
        # Only an immediate decision posts now. `digest` — chosen, or imposed by quiet hours —
        # belongs to the digest sweep, and sending it here as well would send it twice.
        if decision.deliver and decision.mode == "immediate":
            wanted.add(pref.channel)
    return sorted(wanted)


async def fan_out(alert, channels: list[str], registry) -> dict[str, dict]:
    """Post ``alert`` once to each channel and record the outcome on ``alert.meta``. Never raises.

    Outcomes are REDACTED before they are stored: a Slack or Teams webhook URL is the credential,
    Telegram puts its bot token in the path, and httpx writes the full URL into its exceptions.
    """
    from nexus.core.redact import redact

    outcomes: dict[str, dict] = {}
    for name in channels:
        if not registry.has(name):
            outcomes[name] = {"ok": False, "detail": "channel not available"}
            continue
        try:
            result = await registry.get(name).deliver(alert)
            outcomes[name] = {"ok": bool(result.ok), "detail": redact(result.detail or "")}
        except Exception as exc:  # one channel down must not stop the next one
            detail = redact(f"{type(exc).__name__}: {exc}")
            outcomes[name] = {"ok": False, "detail": detail}
            logger.warning("alert %s delivery via %s failed: %s", alert.id, name, detail)
    if outcomes:
        # Reassigned, not mutated in place: SQLAlchemy does not see an in-place change to JSON.
        meta = dict(alert.meta or {})
        meta["deliveries"] = {**(meta.get("deliveries") or {}), **outcomes}
        alert.meta = meta
    return outcomes
