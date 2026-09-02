# tests/test_payment_credentials.py
"""Managing the payment account from the Control plane.

`providers/catalog.py` deliberately kept Stripe out of the generic key pool, and its reason is the
spec for this surface: **money fails silently**. A dead search key returns no results and someone
notices within a day; a wrong Stripe key stops checkout and invoicing, which is indistinguishable
from a quiet month.

So this table carries a rule the key pool does not — a credential cannot go live until a real call
against it has succeeded — and it records WHICH account answered, because authenticating against
the wrong business looks exactly like success.
"""
from __future__ import annotations

import pytest

from tests.conftest import auth, signup


async def _superadmin(client, monkeypatch, *, slug: str, email: str) -> str:
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_admin_emails", email)
    return await signup(client, slug=slug, email=email, company=slug.upper())


async def test_the_secret_key_is_never_returned(client, monkeypatch):
    """Not even to the superadmin who typed it. A panel that can display a payment credential
    leaks it through a screenshot or a support session."""
    token = await _superadmin(client, monkeypatch, slug="pc1", email="boss@pc1.com")
    r = await client.post("/api/admin/payment-credentials", headers=auth(token),
                          json={"label": "main", "secret_key": "sk_test_supersecret_8888",
                                "publishable_key": "pk_test_visible"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert "sk_test_supersecret_8888" not in str(body)
    assert body["key_hint"] == "8888"
    # The publishable key IS returned: it is designed to be shipped to a browser, and sealing it
    # would imply a secrecy it does not have.
    assert body["publishable_key"] == "pk_test_visible"

    listed = (await client.get("/api/admin/payment-credentials", headers=auth(token))).json()
    assert "sk_test_supersecret_8888" not in str(listed)


async def test_a_new_credential_starts_inactive_and_unverified(client, monkeypatch):
    """There is deliberately no add-and-use path for money."""
    token = await _superadmin(client, monkeypatch, slug="pc2", email="boss@pc2.com")
    body = (await client.post("/api/admin/payment-credentials", headers=auth(token),
                              json={"label": "x", "secret_key": "sk_test_1111"})).json()
    assert body["status"] == "registered"
    assert body["active"] is False


async def test_an_unverified_credential_cannot_be_activated(client, monkeypatch):
    """The whole point of the surface.

    A wrong payment key does not raise anywhere an operator looks — checkout sessions stop being
    created and invoices stop being raised. Requiring a live call before activation is the only
    thing standing between "pasted a key" and "silently stopped billing".
    """
    token = await _superadmin(client, monkeypatch, slug="pc3", email="boss@pc3.com")
    row = (await client.post("/api/admin/payment-credentials", headers=auth(token),
                             json={"label": "x", "secret_key": "sk_test_2222"})).json()
    r = await client.post(f"/api/admin/payment-credentials/{row['id']}/activate",
                          headers=auth(token))
    # 409, not 400: the request is well-formed and the credential exists. It is the STATE that
    # forbids it, and the fix is to verify rather than to resend.
    assert r.status_code == 409
    assert "verify" in r.text.lower()


async def test_a_request_body_cannot_set_status_or_active(client, monkeypatch):
    """`extra="forbid"` rejects rather than quietly dropping. An admin who could post
    `status: verified` would walk straight past the live call."""
    token = await _superadmin(client, monkeypatch, slug="pc4", email="boss@pc4.com")
    for smuggled in ({"status": "verified"}, {"active": True}):
        r = await client.post("/api/admin/payment-credentials", headers=auth(token),
                              json={"label": "x", "secret_key": "sk_test_3333", **smuggled})
        assert r.status_code == 422, smuggled


async def test_a_tenant_owner_cannot_reach_it(client):
    token = await signup(client, slug="pc5", email="o@pc5.com", company="PC5")
    assert (await client.get("/api/admin/payment-credentials",
                             headers=auth(token))).status_code == 404


async def test_verification_records_which_account_answered(client, monkeypatch):
    """Authenticating against the WRONG business is the expensive mistake and it looks exactly
    like success. So verification reads the provider's own account object and stores the name."""
    from nexus.billing.payments import StripePaymentProvider

    token = await _superadmin(client, monkeypatch, slug="pc6", email="boss@pc6.com")
    row = (await client.post("/api/admin/payment-credentials", headers=auth(token),
                             json={"label": "live-ish", "secret_key": "sk_test_4444"})).json()

    async def fake_get(self, path):
        assert path == "/account"
        return {"id": "acct_TEST123", "charges_enabled": True,
                "settings": {"dashboard": {"display_name": "Marketjoy Ltd"}}}

    monkeypatch.setattr(StripePaymentProvider, "_get", fake_get)
    r = await client.post(f"/api/admin/payment-credentials/{row['id']}/verify",
                          headers=auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["account_name"] == "Marketjoy Ltd"
    assert r.json()["account_id"] == "acct_TEST123"

    listed = (await client.get("/api/admin/payment-credentials", headers=auth(token))).json()
    stored = next(c for c in listed if c["id"] == row["id"])
    assert stored["status"] == "verified"
    # A test key is never reported as livemode, whatever the account says about charges.
    assert stored["livemode"] is False


async def test_a_verified_credential_activates_and_deactivates_others(client, monkeypatch):
    """Exactly one active credential. Two accounts both collecting money, with no rule about
    which, is an outage you cannot see."""
    from nexus.billing.payments import StripePaymentProvider

    token = await _superadmin(client, monkeypatch, slug="pc7", email="boss@pc7.com")

    async def fake_get(self, path):
        return {"id": "acct_OK", "charges_enabled": True}

    monkeypatch.setattr(StripePaymentProvider, "_get", fake_get)

    ids = []
    for i in (1, 2):
        row = (await client.post("/api/admin/payment-credentials", headers=auth(token),
                                 json={"label": f"acct{i}",
                                       "secret_key": f"sk_test_pool_{i}000"})).json()
        await client.post(f"/api/admin/payment-credentials/{row['id']}/verify",
                          headers=auth(token))
        ids.append(row["id"])

    await client.post(f"/api/admin/payment-credentials/{ids[0]}/activate", headers=auth(token))
    await client.post(f"/api/admin/payment-credentials/{ids[1]}/activate", headers=auth(token))

    listed = (await client.get("/api/admin/payment-credentials", headers=auth(token))).json()
    active = [c["id"] for c in listed if c["active"]]
    assert active == [ids[1]]


async def test_a_failed_verification_deactivates(client, monkeypatch):
    """Leaving a now-broken credential active because it passed last month is how a silent
    billing outage lasts a month."""
    from nexus.billing.payments import PaymentError, StripePaymentProvider

    token = await _superadmin(client, monkeypatch, slug="pc8", email="boss@pc8.com")

    async def ok_get(self, path):
        return {"id": "acct_OK", "charges_enabled": True}

    monkeypatch.setattr(StripePaymentProvider, "_get", ok_get)
    row = (await client.post("/api/admin/payment-credentials", headers=auth(token),
                             json={"label": "rotates", "secret_key": "sk_test_5555"})).json()
    await client.post(f"/api/admin/payment-credentials/{row['id']}/verify", headers=auth(token))
    await client.post(f"/api/admin/payment-credentials/{row['id']}/activate", headers=auth(token))

    async def dead_get(self, path):
        raise PaymentError("stripe /account -> 401: Invalid API Key provided")

    monkeypatch.setattr(StripePaymentProvider, "_get", dead_get)
    r = await client.post(f"/api/admin/payment-credentials/{row['id']}/verify",
                          headers=auth(token))
    assert r.json()["ok"] is False

    listed = (await client.get("/api/admin/payment-credentials", headers=auth(token))).json()
    stored = next(c for c in listed if c["id"] == row["id"])
    assert stored["status"] == "failed"
    assert stored["active"] is False
    # The provider's own words, so an operator can tell a revoked key from a network problem.
    assert "Invalid API Key" in stored["last_error"]


async def test_the_active_credential_cannot_be_deleted(client, monkeypatch):
    """Deleting what the platform is currently billing with, in one click, is not a feature."""
    from nexus.billing.payments import StripePaymentProvider

    token = await _superadmin(client, monkeypatch, slug="pc9", email="boss@pc9.com")

    async def fake_get(self, path):
        return {"id": "acct_OK", "charges_enabled": True}

    monkeypatch.setattr(StripePaymentProvider, "_get", fake_get)
    row = (await client.post("/api/admin/payment-credentials", headers=auth(token),
                             json={"label": "in-use", "secret_key": "sk_test_6666"})).json()
    await client.post(f"/api/admin/payment-credentials/{row['id']}/verify", headers=auth(token))
    await client.post(f"/api/admin/payment-credentials/{row['id']}/activate", headers=auth(token))

    assert (await client.delete(f"/api/admin/payment-credentials/{row['id']}",
                                headers=auth(token))).status_code == 409

    # Deactivating is never refused, and then it deletes.
    await client.post(f"/api/admin/payment-credentials/{row['id']}/deactivate",
                      headers=auth(token))
    assert (await client.delete(f"/api/admin/payment-credentials/{row['id']}",
                                headers=auth(token))).status_code == 204


async def test_no_managed_credential_falls_back_to_the_environment(fresh_db, monkeypatch):
    """Additive by construction: a deployment that never opens this screen behaves exactly as it
    did before the table existed."""
    from nexus.billing.credentials import resolve_stripe_secrets
    from nexus.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "stripe_secret_key", "sk_from_env")
    monkeypatch.setattr(s, "stripe_webhook_secret", "whsec_from_env")

    # The publishable key has no Settings field — it has never been needed server-side — so the
    # environment half of that tuple is empty rather than raising. Reading a setting that does not
    # exist must not take down credential resolution.
    assert await resolve_stripe_secrets() == ("sk_from_env", "whsec_from_env", "")


async def test_an_active_credential_overrides_the_environment(fresh_db, monkeypatch):
    from nexus.billing.credentials import (
        activate_credential,
        add_credential,
        resolve_stripe_secrets,
        verify_credential,
    )
    from nexus.billing.payments import StripePaymentProvider
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_from_env")

    async def fake_get(self, path):
        return {"id": "acct_OK", "charges_enabled": True}

    monkeypatch.setattr(StripePaymentProvider, "_get", fake_get)
    row = await add_credential(label="managed", secret_key="sk_managed_7777",
                               webhook_secret="whsec_managed")
    await verify_credential(row.id)
    await activate_credential(row.id)

    secret, hook, _pub = await resolve_stripe_secrets()
    assert secret == "sk_managed_7777"
    assert hook == "whsec_managed"


async def test_an_empty_secret_key_is_refused(fresh_db):
    """A blank credential would produce a row that looks configured and bills nothing — exactly
    the silent state this whole surface exists to make impossible."""
    from nexus.billing.credentials import CredentialError, add_credential

    with pytest.raises(CredentialError):
        await add_credential(label="blank", secret_key="   ")


async def test_the_managed_webhook_secret_is_what_verifies_events(fresh_db, monkeypatch):
    """Stored and never used is the failure mode this codebase keeps finding.

    The webhook route read `get_settings().stripe_webhook_secret` directly, so a secret typed into
    the Control plane would have been sealed, displayed as configured, and ignored — presenting as
    every Stripe event failing its signature check, which reads as "subscriptions stopped updating".
    """
    from nexus.billing.credentials import (
        activate_credential,
        add_credential,
        resolve_stripe_secrets,
        verify_credential,
    )
    from nexus.billing.payments import StripePaymentProvider
    from nexus.core.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", "whsec_from_env")

    async def fake_get(self, path):
        return {"id": "acct_OK", "charges_enabled": True}

    monkeypatch.setattr(StripePaymentProvider, "_get", fake_get)
    row = await add_credential(label="panel", secret_key="sk_panel_1234",
                               webhook_secret="whsec_typed_in_the_panel")
    await verify_credential(row.id)
    await activate_credential(row.id)

    _sk, hook, _pub = await resolve_stripe_secrets()
    assert hook == "whsec_typed_in_the_panel"

    # And the route reads it through that resolver rather than from settings. Asserted
    # structurally because exercising it needs a signed body: there is no frontend test runner
    # here either, and the same reading-the-source approach is used for the nav/route guards.
    import inspect

    from nexus.api.routers import billing_webhooks

    src = inspect.getsource(billing_webhooks)
    assert "resolve_stripe_secrets" in src
    assert "get_settings().stripe_webhook_secret" not in src
