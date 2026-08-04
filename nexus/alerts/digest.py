# nexus/alerts/digest.py
"""Deliver the alerts that routing held back for a digest.

`routing.py` decides an alert should wait — because the user chose `mode="digest"`, or because it
landed inside their quiet hours. That decision was being recorded and then nothing acted on it. The
alert existed, the routing was correct, and the person was never told: the same shape as the bug
M21 was created to fix, where `signal.created` was published to no subscriber.

Three properties, each of which is the difference between a digest people read and one they filter:

* **A quiet period sends nothing.** An empty digest arriving every morning is how a channel gets
  muted, and then the one that matters is muted too.
* **`last_digest_at` is the watermark**, so a re-run tells nobody the same thing twice. The sweep is
  safe to enqueue on every heartbeat tick.
* **Critical alerts are not here.** They override quiet hours at routing time and go immediately;
  by the time this runs they have already been delivered, and re-sending them in the digest would
  train people that the digest repeats what they already saw.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("nexus.alerts.digest")

# How far back a first-ever digest looks. Without a bound, somebody enabling digests on a
# six-month-old workspace gets their entire alert history in one message.
FIRST_RUN_WINDOW = timedelta(days=1)


@dataclass(slots=True)
class DigestBatch:
    """One person's pending digest."""

    user_id: str
    channel: str = "in_app"
    categories: dict = field(default_factory=dict)
    alert_ids: list = field(default_factory=list)
    window_start: datetime | None = None

    @property
    def count(self) -> int:
        return len(self.alert_ids)

    def summary(self) -> str:
        """A one-line subject. Leads with the number, because that is what decides whether it is
        opened, and names the categories so it is skimmable without opening at all."""
        if not self.alert_ids:
            return ""
        parts = [
            f"{n} {cat.replace('_', ' ')}" for cat, n in
            sorted(self.categories.items(), key=lambda kv: (-kv[1], kv[0]))
        ]
        return f"{self.count} alert{'s' if self.count != 1 else ''}: " + ", ".join(parts[:4])


async def collect_digest(ts, preference, *, now: datetime | None = None) -> DigestBatch:
    """Everything this person should be told since their last digest.

    Reads alerts directly rather than a delivery log: an alert is the fact, and a second table
    recording "we meant to tell them" would drift from it. The watermark is enough.
    """
    from nexus.models.alerts import Alert

    now = now or datetime.now(timezone.utc)
    since = preference.last_digest_at
    if since is None:
        since = now - FIRST_RUN_WINDOW
    elif since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    batch = DigestBatch(
        user_id=preference.user_id,
        channel=preference.channel or "in_app",
        window_start=since,
    )

    alerts = await ts.list(
        Alert,
        Alert.created_at > since,
        # Only what is still outstanding. An alert the rep already opened and dealt with does not
        # need to reappear in a summary the next morning.
        Alert.status == "open",
        limit=200,
    )
    for alert in alerts:
        # `category` lives in `meta`, not a column — `signal_alerts.py` writes it there. Reading a
        # non-existent attribute would silently bucket every alert as "other" and make a
        # category-scoped preference collect everything.
        category = (alert.meta or {}).get("category") or "other"
        # The preference is per category; a row for `funding` must not sweep up `hiring`.
        if preference.category and preference.category != "all" \
                and preference.category != category:
            continue
        batch.alert_ids.append(alert.id)
        batch.categories[category] = batch.categories.get(category, 0) + 1
    return batch


async def run_digest_sweep(ts, *, now: datetime | None = None, send=None) -> dict:
    """Send every due digest for one tenant. Never raises.

    ``send`` is injected so the transport stays a seam — the same reason every provider in this
    repo is one. Default is a log line, which makes the sweep observable without requiring a
    configured channel.
    """
    from nexus.core.config import get_settings
    from nexus.models.notification_preference import NotificationPreference

    now = now or datetime.now(timezone.utc)
    interval = timedelta(hours=max(1, getattr(get_settings(), "digest_interval_hours", 24)))
    result = {"considered": 0, "sent": 0, "empty": 0, "alerts": 0}

    prefs = await ts.list(NotificationPreference, NotificationPreference.mode == "digest",
                          limit=500)
    for pref in prefs:
        last = pref.last_digest_at
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < interval:
                continue          # not due yet; the heartbeat may tick many times per interval
        result["considered"] += 1

        try:
            batch = await collect_digest(ts, pref, now=now)
        except Exception:
            logger.warning("digest collection failed for user %s", pref.user_id, exc_info=True)
            continue

        if batch.count == 0:
            # Quiet period: send nothing, and DO advance the watermark. Not advancing it would make
            # the sweep re-scan the same empty window on every tick forever.
            result["empty"] += 1
            pref.last_digest_at = now
            continue

        try:
            await (send or _log_digest)(ts, pref, batch)
            result["sent"] += 1
            result["alerts"] += batch.count
        except Exception:
            # Delivery failed: leave the watermark alone so the next sweep retries. Advancing it
            # here would silently swallow the one digest that failed to send.
            logger.warning("digest delivery failed for user %s", pref.user_id, exc_info=True)
            continue
        pref.last_digest_at = now

    await ts.flush()
    return result


async def _log_digest(ts, preference, batch: DigestBatch) -> None:
    """Default transport: record it. Real channels register through the notification seam.

    Deliberately not a silent no-op — an unconfigured channel that logs nothing is
    indistinguishable from a sweep that never ran, which is the failure this module exists to fix.
    """
    logger.info(
        "digest for user %s via %s: %s", preference.user_id, batch.channel, batch.summary(),
    )
