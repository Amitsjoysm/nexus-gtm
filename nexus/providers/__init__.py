# nexus/providers/__init__.py
"""Platform-wide provider credentials, managed from the Control plane rather than deploy/.env.

Deployment-wide, not per-tenant: one Exa key serves every workspace. That is the opposite axis from
`nexus/ingestion/crm_credentials.py`, and CLAUDE.md is explicit that conflating the two is not a
config change afterwards — platform data in a tenant table is duplicated N times, tenant data in a
platform table is a cross-tenant leak.
"""
