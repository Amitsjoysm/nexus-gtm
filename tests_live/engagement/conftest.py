"""The live engagement suite: real Google, Microsoft 365, Supabase and LLM calls (D21).

Nothing here is faked. Each test declares the secrets it needs through ``require_env``; when one is
missing the test SKIPS with the variable's name, so a partially configured CI shows exactly what is
left to set up rather than failing.

Run: ``pytest tests_live/engagement -n0 -q -rs``. Serial on purpose: the tests share real mailboxes
and stores, and two runs interleaving sends would make reply matching assertions meaningless.

The database is a throwaway SQLite file, as in ``tests/``; only the provider side is live.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_TMPDIR = tempfile.mkdtemp(prefix="nexus_live_")
os.environ.setdefault("NEXUS_DATABASE_URL", f"sqlite+aiosqlite:///{_TMPDIR}/live.db")
os.environ.setdefault("NEXUS_ENV", "test")
os.environ.setdefault("NEXUS_LLM_PROVIDER", "auto")


def require_env(*names: str) -> dict[str, str]:
    """The named environment values, or skip the test naming whichever are missing."""
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        pytest.skip(f"live secret not configured: {', '.join(missing)}")
    return values


def redirect_uri(provider: str) -> str:
    base = require_env("NEXUS_LIVE_REDIRECT_BASE")["NEXUS_LIVE_REDIRECT_BASE"].rstrip("/")
    return f"{base}/api/engagement/mailboxes/oauth/{provider}/callback"
