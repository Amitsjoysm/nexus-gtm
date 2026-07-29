# tests/test_rls_binding_guard.py
"""Structural guard: a hand-built TenantSession must bind the tenant GUC.

The API runs as the least-privilege ``nexus_app`` role with Postgres RLS enforced. A
``TenantSession`` constructed on a raw session does NOT bind ``app.current_tenant``, so any write
through it is rejected with "new row violates row-level security policy".

This is invisible to the rest of the suite: tests run on SQLite, which has no RLS, so the code
passes every functional test and fails only against the real database. That is exactly what
happened to the subscription backfill and both admin write endpoints. A behavioural test cannot
catch it offline, so this one checks the structure instead.

Approved constructions live behind helpers that already bind: the request dependency in
``nexus/api/deps.py`` and the worker's ``tenant_session`` context manager.
"""
from __future__ import annotations

import ast
from pathlib import Path

NEXUS = Path(__file__).resolve().parents[1] / "nexus"

# Files whose TenantSession construction is the binding helper itself.
_EXEMPT = {
    Path("api") / "deps.py",
    Path("workers") / "tasks.py",
    Path("core") / "tenancy.py",
}


def _constructs_tenant_session(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "TenantSession"
            and sub.args  # TenantSession(session, tid) — a raw construction
        ):
            return True
    return False


def _calls_apply_rls(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Name) and fn.id == "apply_rls":
                return True
            if isinstance(fn, ast.Attribute) and fn.attr == "apply_rls":
                return True
    return False


def test_every_hand_built_tenant_session_binds_rls():
    offenders: list[str] = []

    for path in NEXUS.rglob("*.py"):
        rel = path.relative_to(NEXUS)
        if rel in _EXEMPT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - defensive
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _constructs_tenant_session(node) and not _calls_apply_rls(node):
                offenders.append(f"{rel.as_posix()}::{node.name}")

    assert not offenders, (
        "These build a TenantSession on a raw session without calling apply_rls(). "
        "Writes through them are rejected by Postgres RLS in production, and SQLite will not "
        "catch it: " + ", ".join(sorted(offenders))
    )
