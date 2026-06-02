"""List Builder: rep-friendly prospecting lists from firmographic + score filters."""
from __future__ import annotations

from sqlalchemy import func, select

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account
from nexus.models.intelligence import AccountScore
from nexus.models.workflow import ListItem, ProspectList


class ListBuilder:
    async def _matching_accounts(
        self, ts: TenantSession, flt: dict, *, limit: int | None = None
    ) -> list[Account]:
        where = []
        if flt.get("industries"):
            where.append(Account.industry.in_(flt["industries"]))
        if flt.get("countries"):
            where.append(Account.country.in_(flt["countries"]))
        if flt.get("min_employees") is not None:
            where.append(Account.employee_count >= flt["min_employees"])
        if flt.get("max_employees") is not None:
            where.append(Account.employee_count <= flt["max_employees"])

        min_composite = flt.get("min_composite")
        if min_composite is None:
            # No score filter: firmographics + LIMIT pushed straight to SQL.
            return await ts.list(Account, *where, limit=limit)

        # Score-filtered: join each account to its latest score in the DB rather than
        # loading every account and its full score history into Python. The subquery
        # picks the newest computed_at per account; the join back to AccountScore reads
        # that row's composite. group_by keeps this SQLite-compatible for the test suite.
        latest = (
            select(
                AccountScore.account_id.label("account_id"),
                func.max(AccountScore.computed_at).label("latest_at"),
            )
            .where(AccountScore.tenant_id == ts.tenant_id)
            .group_by(AccountScore.account_id)
            .subquery()
        )
        stmt = (
            ts.select(Account, *where)
            .join(latest, latest.c.account_id == Account.id)
            .join(
                AccountScore,
                (AccountScore.account_id == latest.c.account_id)
                & (AccountScore.computed_at == latest.c.latest_at),
            )
            .where(AccountScore.composite >= min_composite)
            # AccountScore has no unique (account_id, computed_at); two score rows
            # sharing the same max timestamp would otherwise emit duplicate Account
            # rows (→ duplicate ListItems in build()). DISTINCT collapses the tie.
            .distinct()
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await ts.session.scalars(stmt)).all())

    async def preview(self, ts: TenantSession, flt: dict, *, limit: int = 50) -> list[Account]:
        return await self._matching_accounts(ts, flt, limit=limit)

    async def build(
        self, ts: TenantSession, *, name: str, flt: dict, owner_user_id: str | None = None
    ) -> tuple[ProspectList, int]:
        plist = ProspectList(
            tenant_id=ts.tenant_id, name=name, owner_user_id=owner_user_id, filter=flt
        )
        ts.add(plist)
        await ts.flush()
        accounts = await self._matching_accounts(ts, flt)
        for acc in accounts:
            ts.add(ListItem(tenant_id=ts.tenant_id, list_id=plist.id, account_id=acc.id))
        await ts.flush()
        return plist, len(accounts)


_builder = ListBuilder()


def get_list_builder() -> ListBuilder:
    return _builder
