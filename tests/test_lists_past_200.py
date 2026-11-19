"""Accounts and Contacts past 200 rows (asked 2026-11-19).

"New companies finding and new contacts fetching fail after 200 accounts." One cause was the
screens, not discovery: both pages read the newest 200 rows and filtered, sorted, counted and
selected in the browser, so everything older could not be seen, found or picked. They now read
every page (`ApiClient.pagedList`) and draw 200 at a time (`DataTable` ``pageSize``), and the server
orders with a tiebreaker, because an import's rows can share one timestamp and offset pages over an
order with ties overlap and skip.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import insert

from nexus.core.security import create_access_token
from tests.conftest import auth, make_tenant

FRONTEND = Path(__file__).resolve().parents[1] / "frontend" / "src"


async def test_pages_over_rows_with_one_timestamp_return_each_row_once(client):
    from nexus.core.db import get_platform_sessionmaker
    from nexus.models.account import Account, Contact

    tid = await make_tenant("lp1", "LP1")
    same = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
    async with get_platform_sessionmaker()() as s:
        await s.execute(insert(Account), [
            {"id": f"acc{i:04d}", "tenant_id": tid, "name": f"A{i}", "domain": f"a{i}.io",
             "created_at": same, "updated_at": same} for i in range(31)])
        await s.execute(insert(Contact), [
            {"id": f"con{i:04d}", "tenant_id": tid, "account_id": f"acc{i % 31:04d}",
             "full_name": f"P {i}", "created_at": same, "updated_at": same} for i in range(31)])
        await s.commit()
    h = auth(create_access_token(user_id="u-mgr", tenant_id=tid, role="manager"))
    for path in ("/api/accounts", "/api/contacts"):
        ids: list[str] = []
        for offset in range(0, 31, 7):
            r = await client.get(path, headers=h, params={"limit": 7, "offset": offset})
            assert r.status_code == 200, r.text
            ids += [row["id"] for row in r.json()]
        assert len(ids) == 31 and len(set(ids)) == 31, (path, len(ids), len(set(ids)))


def test_both_pages_read_every_row_and_draw_two_hundred_at_a_time():
    accounts = (FRONTEND / "pages" / "AccountsPage.tsx").read_text(encoding="utf-8")
    contacts = (FRONTEND / "pages" / "ContactsPage.tsx").read_text(encoding="utf-8")
    assert "api.listAllAccounts(signal)" in accounts and "pageSize={ROWS_DRAWN}" in accounts
    assert "api.listAllWorkspaceContacts(signal)" in contacts and "pageSize={200}" in contacts


def test_the_table_sorts_every_row_and_draws_a_page():
    table = (FRONTEND / "components" / "ui" / "DataTable.tsx").read_text(encoding="utf-8")
    assert "sortedRows.slice(0, shown)" in table, "the page is the top of the WHOLE sort"
    assert "drawn.map(" in table and "Show {Math.min(pageSize, hidden)" in table


def test_paging_deduplicates_and_the_dnc_check_covers_every_row():
    api = (FRONTEND / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "private async pagedList<" in api and "seen.has(row.id)" in api
    dnc = (FRONTEND / "components" / "engagement" / "DncBadge.tsx").read_text(encoding="utf-8")
    assert ".slice(0, 500).join" not in dnc, "only the first 500 addresses were checked"
    assert "i += 500" in dnc
