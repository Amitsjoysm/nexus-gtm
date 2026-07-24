"""Alert intelligence enrichment — deterministic, structured meta fields."""
from __future__ import annotations

from datetime import datetime, timezone

from nexus.alerts.enrichment import build_alert_intelligence
from nexus.models.account import Account
from nexus.models.signal import SignalEvent


def _sig(kind="funding", strength=0.85, source="web_news", url="https://x.com/a", body="raised $10M"):
    return SignalEvent(
        kind=kind, source=source, title="Acme raised $10M", body=body, url=url,
        strength=strength, dedupe_key="k", occurred_at=datetime.now(timezone.utc),
    )


def _acct():
    return Account(name="Acme", industry="Software", employee_count=800, domain="acme.com")


def test_funding_intelligence_shape():
    meta = build_alert_intelligence(_acct(), _sig(), composite=70)
    assert meta["category"] == "Funding"
    assert 0 <= meta["importance"] <= 100
    assert 0.0 <= meta["confidence"] <= 1.0
    assert meta["source_url"] == "https://x.com/a"
    assert "ICP fit 70" in meta["matched_icp"]
    for key in ("suggested_action", "next_best_action", "ai_insight", "reason", "summary",
                "signal_kind"):
        assert meta[key], f"missing {key}"


def test_importance_rises_with_strength_and_fit():
    low = build_alert_intelligence(_acct(), _sig(strength=0.4), composite=30)["importance"]
    high = build_alert_intelligence(_acct(), _sig(strength=0.9), composite=90)["importance"]
    assert high > low


def test_confidence_lower_for_synthetic_source():
    real = build_alert_intelligence(_acct(), _sig(source="web_news"), composite=50)["confidence"]
    demo = build_alert_intelligence(_acct(), _sig(source="demo"), composite=50)["confidence"]
    assert real > demo


def test_unknown_kind_uses_default_playbook():
    meta = build_alert_intelligence(_acct(), _sig(kind="mystery"), composite=50)
    assert meta["category"] == "Signal"
    assert meta["suggested_action"]


def test_missing_composite_defaults_to_neutral_fit():
    meta = build_alert_intelligence(_acct(), _sig(), composite=None)
    assert "ICP fit 50" in meta["matched_icp"]
