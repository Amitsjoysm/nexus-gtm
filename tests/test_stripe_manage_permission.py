# tests/test_stripe_manage_permission.py
"""`stripe.manage`: creating and changing objects at Stripe is the superadmin's alone (2026-10-09)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from nexus.billing.permissions import (
    ALL_PERMISSIONS,
    FEATURES_MANAGE,
    PERMISSIONS_BEFORE_FEATURES_MANAGE,
    PERMISSIONS_BEFORE_STRIPE_MANAGE,
    ROLE_PRESETS,
    STRIPE_MANAGE,
    widened_with,
)

ROOT = Path(__file__).resolve().parents[1]


def test_only_the_superadmin_preset_holds_it():
    assert STRIPE_MANAGE == "stripe.manage"
    assert STRIPE_MANAGE in ALL_PERMISSIONS
    assert STRIPE_MANAGE in ROLE_PRESETS["superadmin"]
    assert STRIPE_MANAGE not in ROLE_PRESETS["finance"]
    assert STRIPE_MANAGE not in ROLE_PRESETS["support"]


def test_the_features_manage_backfill_set_stays_frozen():
    """0055 matched rows against the permissions that existed before `features.manage`. Deriving
    that set from ALL_PERMISSIONS would now include `stripe.manage`, and a database upgrading
    across both migrations would get neither grant."""
    assert FEATURES_MANAGE not in PERMISSIONS_BEFORE_FEATURES_MANAGE
    assert STRIPE_MANAGE not in PERMISSIONS_BEFORE_FEATURES_MANAGE
    assert set(PERMISSIONS_BEFORE_STRIPE_MANAGE) == set(ALL_PERMISSIONS) - {STRIPE_MANAGE}


def test_a_full_admin_gains_the_new_permission():
    held = list(PERMISSIONS_BEFORE_STRIPE_MANAGE)
    assert widened_with(held, STRIPE_MANAGE, PERMISSIONS_BEFORE_STRIPE_MANAGE) == sorted(
        set(held) | {STRIPE_MANAGE}
    )


def test_a_narrowed_admin_stays_narrowed():
    held = [p for p in PERMISSIONS_BEFORE_STRIPE_MANAGE if p != "pricing.write"]
    assert widened_with(held, STRIPE_MANAGE, PERMISSIONS_BEFORE_STRIPE_MANAGE) is None


def test_an_empty_list_and_an_existing_grant_are_left_alone():
    assert widened_with([], STRIPE_MANAGE, PERMISSIONS_BEFORE_STRIPE_MANAGE) is None
    assert widened_with(list(ALL_PERMISSIONS), STRIPE_MANAGE,
                        PERMISSIONS_BEFORE_STRIPE_MANAGE) is None


def test_the_migration_sits_on_the_previous_head():
    path = ROOT / "migrations" / "versions" / "0067_stripe_manage.py"
    spec = importlib.util.spec_from_file_location("m0067", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0067_stripe_manage"
    assert mod.down_revision == "0066_page_visibility"


def test_the_console_names_the_permission():
    src = (ROOT / "frontend" / "src" / "lib" / "permissions.ts").read_text(encoding="utf-8")
    assert 'export const STRIPE_MANAGE = "stripe.manage";' in src
    assert "[STRIPE_MANAGE]:" in src
