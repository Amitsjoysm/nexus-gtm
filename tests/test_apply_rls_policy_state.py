"""How `scripts/apply_rls.py` decides a tenant table already enforces its policy.

The decision is what lets a boot skip the ACCESS EXCLUSIVE locks that kept v1.13.0 from starting on
staging (2026-11-06), so it must be exact both ways: a table that is right is left alone, and a
table that is wrong in any way is set up again. The catalog stores the policy in Postgres's own
normal form, which is what these strings are (read from a real Postgres 16). The Postgres half,
which runs the script itself, is `tests_integration/test_apply_rls_reboot.py`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def _script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "apply_rls.py"
    spec = importlib.util.spec_from_file_location("_apply_rls_state", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STORED = "((tenant_id)::text = current_setting('app.current_tenant'::text, true))"


def _row(**overrides):
    row = dict(rls=True, has_policy=True, cmd="*", permissive=True, to_public=True,
               qual=STORED, with_check=STORED)
    row.update(overrides)
    return SimpleNamespace(**row)


def test_the_stored_form_of_our_policy_is_in_place():
    script = _script()
    assert script.policy_in_place(_row())
    # A varchar column with a length casts differently; it is still the same policy.
    other = "((tenant_id)::character varying(32) = current_setting('app.current_tenant'::text, true))"
    assert script.policy_in_place(_row(qual=other, with_check=other))


@pytest.mark.parametrize("change", [
    {"rls": False},
    {"has_policy": False, "cmd": None, "permissive": None, "to_public": None,
     "qual": None, "with_check": None},
    {"cmd": "r"},
    {"permissive": False},
    {"to_public": False},
    {"qual": "true"},
    {"with_check": None},
    {"qual": "((tenant_id)::text = current_setting('app.other_tenant'::text, true))"},
    {"with_check": "((owner_id)::text = current_setting('app.current_tenant'::text, true))"},
])
def test_anything_else_is_set_up_again(change):
    assert not _script().policy_in_place(_row(**change))


def test_the_set_up_is_one_statement():
    sql = _script()._set_up_sql("accounts")
    assert sql.startswith("DO $$ BEGIN ") and sql.endswith("END $$;")
    assert sql.count("tenant_id = current_setting('app.current_tenant', true)") == 2
    assert 'ALTER TABLE "accounts" ENABLE ROW LEVEL SECURITY' in sql
