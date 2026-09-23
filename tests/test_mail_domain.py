"""Which domain the organisation actually receives email on.

Every guess used to be built on the website domain with nothing checking that mail goes there.
All evidence here is injected: the suite never touches DNS or the network.
"""
from __future__ import annotations

from nexus.enrichment.mail_domain import (
    CACHE_KEY,
    infer_format_index,
    mail_domain_of,
    resolve_mail_domain,
)
from nexus.models.account import Account, Contact
from nexus.verification import STATUS_VALID
from tests.conftest import make_tenant, tenant_session


def _fetch(final_host="", published=()):
    calls: list[str] = []

    async def fetch(domain: str):
        calls.append(domain)
        return final_host or domain, list(published)

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def _mx(*accepting: str):
    accepts = {d.lower() for d in accepting}

    async def mx(domain: str) -> bool:
        return domain.lower() in accepts

    return mx


async def _account(ts, tid, domain="acme.io", contacts=()):
    acc = Account(tenant_id=tid, name="Acme", domain=domain)
    ts.add(acc)
    await ts.flush()
    for full_name, email, status in contacts:
        ts.add(Contact(tenant_id=tid, account_id=acc.id, full_name=full_name, email=email,
                       email_status=status))
    await ts.flush()
    return acc


async def test_a_verified_colleague_settles_it():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _account(ts, tid, contacts=[("Al Smith", "al@acme.com", STATUS_VALID)])
        fetch = _fetch()
        # No MX anywhere: a colleague's verified address is proof by itself.
        resolved = await resolve_mail_domain(ts, acc, fetch=fetch, mx=_mx())

    assert (resolved.domain, resolved.source) == ("acme.com", "verified_contacts")
    assert mail_domain_of(acc) == "acme.com"


async def test_an_address_published_on_the_site_beats_a_website_domain_that_takes_no_mail():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _account(ts, tid)
        resolved = await resolve_mail_domain(
            ts, acc, fetch=_fetch(published=["hello@acme.com"]), mx=_mx("acme.com"),
        )

    assert (resolved.domain, resolved.source) == ("acme.com", "website_emails")
    assert resolved.published == ("hello@acme.com",)


async def test_a_vendors_address_on_the_page_is_ignored():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _account(ts, tid)
        resolved = await resolve_mail_domain(
            ts, acc,
            fetch=_fetch(published=["privacy@onetrust.com", "noreply@acme.com"]),
            mx=_mx("acme.io"),
        )

    assert resolved.domain == "acme.io", "a third party's address set the company's mail domain"
    assert resolved.published == ()


async def test_a_redirect_target_is_used_when_the_website_domain_takes_no_mail():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _account(ts, tid)
        resolved = await resolve_mail_domain(
            ts, acc, fetch=_fetch(final_host="acme.com"), mx=_mx("acme.com"),
        )

    assert (resolved.domain, resolved.source) == ("acme.com", "website_redirect")


async def test_the_website_domain_is_used_when_it_takes_mail():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _account(ts, tid)
        resolved = await resolve_mail_domain(ts, acc, fetch=_fetch(), mx=_mx("acme.io"))

    assert (resolved.domain, resolved.source) == ("acme.io", "website_domain")


async def test_nothing_takes_mail_means_nothing_is_guessed():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _account(ts, tid)
        resolved = await resolve_mail_domain(ts, acc, fetch=_fetch(), mx=_mx())

    assert (resolved.domain, resolved.source) == ("", "none")
    assert mail_domain_of(acc) == "", "the finder would guess ten addresses at a dead domain"


async def test_the_answer_is_cached_and_re_resolved_on_demand():
    tid = await make_tenant()
    async with tenant_session(tid) as ts:
        acc = await _account(ts, tid)
        fetch = _fetch()
        await resolve_mail_domain(ts, acc, fetch=fetch, mx=_mx("acme.io"))
        await resolve_mail_domain(ts, acc, fetch=fetch, mx=_mx("acme.io"))
        assert len(fetch.calls) == 1, "the site was fetched again inside the cache window"

        await resolve_mail_domain(ts, acc, fetch=fetch, mx=_mx("acme.io"), force=True)
        assert len(fetch.calls) == 2


async def test_a_private_host_is_never_fetched(monkeypatch):
    # A domain is tenant-typed input; fetching whatever it names is an SSRF primitive.
    import socket

    from nexus.enrichment import mail_domain as module

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))],
    )

    # The guard returns before httpx is ever reached, so nothing can be requested.
    assert await module.fetch_site("internal.acme.io") == ("", [])


def test_the_company_format_is_inferred_from_verified_colleagues():
    people = [
        Contact(tenant_id="t", account_id="a", full_name="Al Smith", email="asmith@acme.com",
                email_status=STATUS_VALID),
        Contact(tenant_id="t", account_id="a", full_name="Bea Jones", email="bjones@acme.com",
                email_status=STATUS_VALID),
        Contact(tenant_id="t", account_id="a", full_name="Cy Ray", email="cy.ray@acme.com",
                email_status=None),  # unverified: no vote
    ]
    from nexus.enrichment.providers import name_patterns

    assert infer_format_index(people) == name_patterns("Al Smith").index("asmith")


def test_no_verified_colleague_means_no_format():
    assert infer_format_index([]) is None
