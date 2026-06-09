"""Campaign contact-sourcing: model fields, schemas, draft retry, send policy. Offline."""
from __future__ import annotations

from nexus.models.campaign import (
    SKIP_RISKY,
    SKIP_UNVERIFIED,
    Campaign,
)
from nexus.campaigns.schemas import CampaignIn, CampaignOut
from tests.conftest import make_tenant, tenant_session


def test_new_skip_reason_constants():
    assert SKIP_UNVERIFIED == "unverified_contact"
    assert SKIP_RISKY == "risky_address"


async def test_campaign_send_risky_defaults_false():
    async with tenant_session(await make_tenant()) as ts:
        c = Campaign(tenant_id=ts.tenant_id, name="C", list_id="l1")
        ts.add(c)
        await ts.flush()
        assert c.send_risky is False


def test_schema_in_accepts_send_risky_default_false():
    body = CampaignIn(name="C", list_id="l1")
    assert body.send_risky is False
    assert CampaignIn(name="C", list_id="l1", send_risky=True).send_risky is True


async def test_schema_out_exposes_send_risky():
    async with tenant_session(await make_tenant()) as ts:
        c = Campaign(tenant_id=ts.tenant_id, name="C", list_id="l1", send_risky=True)
        ts.add(c)
        await ts.flush()
        out = CampaignOut.from_model(c)
    assert out.send_risky is True
