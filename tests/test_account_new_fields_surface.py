# tests/test_account_new_fields_surface.py
"""The region / postal / revenue columns must be visible through the API.

Caught by a live smoke test after deploying, not by any unit test: a CSV import wrote
`region="California"` and `annual_revenue=25000000` onto a real account, the import reported
`created: 1`, and `GET /accounts` returned `region: None, annual_revenue: None`.

`AccountOut` already had a `region` field and a `revenue` field, both populated from
`custom_fields` by enrichment — so adding columns of the same name left the API reading the old
source and the new data invisible. The ICP could filter on values nobody could see or correct,
which is worse than not having the filter.

`revenue` (an enrichment BAND like "$10M-$50M") and `annual_revenue` (an exact figure the ICP
scores a numeric range against) stay separate fields on purpose.
"""
from __future__ import annotations

from nexus.models.account import Account


def _out(account):
    from nexus.api.routers.accounts import _account_out

    return _account_out(account)


def test_the_stored_columns_are_returned():
    out = _out(Account(
        id="a1", tenant_id="t1", name="Acme", domain="acme.com",
        region="California", postal_code="94107", annual_revenue=25_000_000,
    ))
    assert out.region == "California"
    assert out.postal_code == "94107"
    assert out.annual_revenue == 25_000_000


def test_enrichment_values_still_surface_when_no_column_is_set():
    """Regression guard: `region` was populated ONLY from custom_fields before the column existed,
    and every already-enriched account in the estate still carries it there."""
    out = _out(Account(
        id="a1", tenant_id="t1", name="Acme", domain="acme.com",
        custom_fields={"region": "Bavaria", "postal_code": "80331", "revenue": "$10M-$50M"},
    ))
    assert out.region == "Bavaria"
    assert out.postal_code == "80331"
    assert out.revenue == "$10M-$50M"


def test_the_column_wins_over_the_enrichment_guess():
    """The column is what an operator typed or imported; the custom field is what a provider
    inferred. When they disagree, the human is right."""
    out = _out(Account(
        id="a1", tenant_id="t1", name="Acme", domain="acme.com", region="California",
        custom_fields={"region": "Bavaria"},
    ))
    assert out.region == "California"


def test_the_band_and_the_exact_figure_are_separate_fields():
    """Folding an exact number into the band string would make one field mean two different things
    depending on where the value came from."""
    out = _out(Account(
        id="a1", tenant_id="t1", name="Acme", domain="acme.com", annual_revenue=25_000_000,
        custom_fields={"revenue": "$10M-$50M"},
    ))
    assert out.revenue == "$10M-$50M"
    assert out.annual_revenue == 25_000_000


def test_an_account_with_neither_returns_nulls():
    out = _out(Account(id="a1", tenant_id="t1", name="Acme", domain="acme.com"))
    assert out.region is None
    assert out.postal_code is None
    assert out.annual_revenue is None
