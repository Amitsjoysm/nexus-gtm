"""The storage LinkedIn prospecting needs, and where each piece lives.

* LinkedIn facts about a company go on the SHARED ``companies`` row, because the company-search
  actor's answer is the same for every workspace: bought once, served to all.
* ``prospect_cursors`` records how far through one actor query we have read. It is platform-global
  for the same reason: every page read is stored in the shared table, so a second workspace with the
  same ICP gets those companies from the database step and must never pay to read the page again.
* ``prospect_runs`` is one workspace's populate request, so it is tenant-scoped and RLS-enrolled.
"""
from __future__ import annotations

from alembic.config import Config
from alembic.script import ScriptDirectory

from nexus.models import Company, ProspectCursor, ProspectRun


def test_linkedin_facts_live_on_the_shared_company_row():
    cols = Company.__table__.columns
    for name in ("linkedin_url", "linkedin_id", "linkedin_industry_id", "employee_range_min",
                 "employee_range_max", "hq_country_code", "description", "linkedin_fetched_at",
                 "similar_linkedin"):
        assert name in cols, name
        assert cols[name].nullable, f"{name} must be nullable: every existing company lacks it"
    assert "tenant_id" not in cols


def test_the_database_step_can_filter_by_industry_and_country_on_an_index():
    indexed = {tuple(c.name for c in ix.columns) for ix in Company.__table__.indexes}
    assert ("linkedin_industry_id", "hq_country_code") in indexed
    assert ("linkedin_url",) in indexed, "similar companies resolve by LinkedIn URL"


def test_a_cursor_is_shared_and_a_run_belongs_to_one_workspace():
    assert "tenant_id" not in ProspectCursor.__table__.columns, (
        "a per-tenant cursor re-buys pages another workspace already stored"
    )
    assert "tenant_id" in ProspectRun.__table__.columns, "apply_rls.py enrols by tenant_id"


def test_there_is_exactly_one_alembic_head():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0061_linkedin_prospecting"]
