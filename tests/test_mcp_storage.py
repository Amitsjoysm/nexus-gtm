# tests/test_mcp_storage.py
"""Storage, settings and the module gate for the MCP server (spec 2026-10-13, task 1)."""
from __future__ import annotations

import pytest

MCP_TABLES = ("mcp_connections", "oauth_clients", "oauth_codes", "oauth_tokens")


def test_the_mcp_tables_carry_no_tenant_id():
    """A bearer is resolved before any workspace is known, so these tables must stay out of RLS:
    `apply_rls.py` enrols every table with a `tenant_id`, and under a policy the lookup would
    return zero rows rather than fail."""
    import nexus.models  # noqa: F401
    from nexus.core.db import Base

    for name in MCP_TABLES:
        assert name in Base.metadata.tables, name
        assert "tenant_id" not in Base.metadata.tables[name].columns, name
    assert "subject_tenant_id" in Base.metadata.tables["mcp_connections"].columns
    assert "subject_tenant_id" in Base.metadata.tables["oauth_codes"].columns


def test_module_mcp_is_enabled_by_default_and_on_no_plan():
    from nexus.billing.catalog import CAPABILITY_SEED
    from nexus.billing import plans

    cap = next(c for c in CAPABILITY_SEED if c["id"] == "module.mcp")
    assert cap["default_mode"] == "enabled"
    seeded = repr(getattr(plans, "PLAN_SEED", "")) + repr(getattr(plans, "ENTITLEMENT_SEED", ""))
    assert "module.mcp" not in seeded


def test_the_three_settings_are_in_the_panel():
    from nexus.runtime_config.catalog import ACCESS, CATALOG

    for key in ("mcp_rate_limit_per_minute", "mcp_allowed_origins", "openai_apps_challenge_token"):
        assert key in CATALOG, key
        assert CATALOG[key].group == ACCESS


@pytest.mark.parametrize("value", ["", "https://a.example.com", "https://a.example.com, https://b.example.com:8443"])
def test_allowed_origins_accepts_https_origins(value):
    from nexus.runtime_config.service import validate

    validate("mcp_allowed_origins", value)


@pytest.mark.parametrize("value", ["http://x.com", "https://x.com/path", "*", "https://", "x.com"])
def test_allowed_origins_refuses_anything_else(value):
    from nexus.runtime_config.service import validate

    with pytest.raises(ValueError):
        validate("mcp_allowed_origins", value)


@pytest.mark.parametrize("value", ["", "abc-DEF_123.xyz", "a" * 200])
def test_challenge_token_accepts_a_plain_token(value):
    from nexus.runtime_config.service import validate

    validate("openai_apps_challenge_token", value)


@pytest.mark.parametrize("value", ["has space", "<script>", "a" * 201])
def test_challenge_token_refuses_markup_and_length(value):
    from nexus.runtime_config.service import validate

    with pytest.raises(ValueError):
        validate("openai_apps_challenge_token", value)
