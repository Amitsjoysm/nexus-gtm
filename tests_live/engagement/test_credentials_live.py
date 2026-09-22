"""The Test buttons for the two OAuth client secrets, against the real Google and Microsoft apps."""
from __future__ import annotations

from nexus.engagement.credential_checks import check_google_client, check_microsoft_client
from tests_live.engagement.conftest import redirect_uri, require_env


async def test_the_real_google_client_is_recognised_and_a_wrong_secret_is_not():
    env = require_env("NEXUS_LIVE_GOOGLE_CLIENT_ID", "NEXUS_LIVE_GOOGLE_CLIENT_SECRET")
    uri = redirect_uri("google")

    good = await check_google_client(
        env["NEXUS_LIVE_GOOGLE_CLIENT_SECRET"], client_id=env["NEXUS_LIVE_GOOGLE_CLIENT_ID"],
        redirect_uri=uri,
    )
    assert good.ok and good.status == "probe_ok", good.detail

    bad = await check_google_client(
        "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0", client_id=env["NEXUS_LIVE_GOOGLE_CLIENT_ID"],
        redirect_uri=uri,
    )
    assert not bad.ok and bad.http_status == 401, (bad.http_status, bad.detail)


async def test_the_real_microsoft_client_is_recognised_and_a_wrong_secret_is_not():
    env = require_env(
        "NEXUS_LIVE_MICROSOFT_CLIENT_ID", "NEXUS_LIVE_MICROSOFT_CLIENT_SECRET",
        "NEXUS_LIVE_MICROSOFT_TENANT",
    )
    uri = redirect_uri("microsoft")

    good = await check_microsoft_client(
        env["NEXUS_LIVE_MICROSOFT_CLIENT_SECRET"], client_id=env["NEXUS_LIVE_MICROSOFT_CLIENT_ID"],
        tenant=env["NEXUS_LIVE_MICROSOFT_TENANT"], redirect_uri=uri,
    )
    assert good.ok and good.status == "probe_ok", good.detail

    bad = await check_microsoft_client(
        "not~the~secret~value~0000000000000000", client_id=env["NEXUS_LIVE_MICROSOFT_CLIENT_ID"],
        tenant=env["NEXUS_LIVE_MICROSOFT_TENANT"], redirect_uri=uri,
    )
    assert not bad.ok and "AADSTS7000215" in bad.detail, bad.detail
