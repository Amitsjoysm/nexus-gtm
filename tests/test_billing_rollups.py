# tests/test_billing_rollups.py
from __future__ import annotations

from datetime import datetime, timezone


def test_period_keys_are_stable_and_sortable():
    from nexus.billing.rollups import period_key

    ts = datetime(2026, 7, 28, 14, 37, 12, tzinfo=timezone.utc)
    assert period_key(ts, "hour") == "2026-07-28T14"
    assert period_key(ts, "day") == "2026-07-28"
    assert period_key(ts, "period") == "2026-07"
    # Lexical sort == chronological sort (so range scans work without parsing).
    earlier = period_key(datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc), "hour")
    assert earlier < period_key(ts, "hour")


def test_period_key_normalises_naive_datetimes_to_utc():
    """SQLite returns naive datetimes; a naive value must not shift the bucket."""
    from nexus.billing.rollups import period_key

    naive = datetime(2026, 7, 28, 14, 37, 12)
    assert period_key(naive, "hour") == "2026-07-28T14"
