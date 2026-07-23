"""Multi-tenant isolation.

Two layers of defense:

1. :class:`TenantSession` — an application-level guard that stamps ``tenant_id`` on inserts,
   filters every read by the active tenant, and validates ownership on update/delete. Works on
   any database (including SQLite used in tests).
2. Postgres Row-Level Security — :func:`apply_rls` issues ``SET LOCAL`` so RLS policies enforce
   isolation at the database, independent of application code.

A ``before_flush`` listener is the backstop: any tenant-scoped object reaching the DB without a
matching ``tenant_id`` raises :class:`TenancyViolation` rather than leaking across tenants.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Sequence, TypeVar

from sqlalchemy import String, event, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.sql import Select


_current_tenant: ContextVar[str | None] = ContextVar("current_tenant", default=None)


class TenancyViolation(Exception):
    """Raised when an operation would cross a tenant boundary."""


def set_current_tenant(tenant_id: str | None) -> None:
    _current_tenant.set(tenant_id)


def get_current_tenant() -> str | None:
    return _current_tenant.get()


def require_current_tenant() -> str:
    tid = _current_tenant.get()
    if not tid:
        raise TenancyViolation("No active tenant in context")
    return tid


class TenantScoped:
    """Mixin marking a model as belonging to exactly one tenant."""

    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)


ModelT = TypeVar("ModelT", bound="TenantScoped")


@event.listens_for(Session, "before_flush")
def _enforce_tenant_on_flush(session: Session, flush_context, instances) -> None:
    """Backstop: stamp/validate tenant_id on every tenant-scoped object being persisted.

    The active tenant is resolved from the *flushing session itself* (``session.info``, stamped
    by :class:`TenantSession`) first, falling back to the process-global context var. Binding to
    the session — not just a single global — means two tenant sessions open in the same context
    (e.g. an isolation test, or any code holding two sessions) each validate against their own
    tenant instead of whichever set the global last. Tenant-less raw sessions (signup/login) have
    no ``session.info`` entry and fall through to the context var (``None``) exactly as before.
    """
    tid = session.info.get("tenant_id") or _current_tenant.get()
    for obj in session.new:
        if isinstance(obj, TenantScoped):
            if getattr(obj, "tenant_id", None) is None:
                if not tid:
                    raise TenancyViolation(
                        f"Inserting {type(obj).__name__} without a tenant in context"
                    )
                obj.tenant_id = tid
            elif tid and obj.tenant_id != tid:
                raise TenancyViolation(
                    f"{type(obj).__name__}.tenant_id={obj.tenant_id} != active tenant {tid}"
                )
    for obj in session.dirty:
        if isinstance(obj, TenantScoped) and tid and obj.tenant_id != tid:
            raise TenancyViolation(
                f"Updating {type(obj).__name__} owned by {obj.tenant_id} as tenant {tid}"
            )
    for obj in session.deleted:
        if isinstance(obj, TenantScoped) and tid and obj.tenant_id != tid:
            raise TenancyViolation(
                f"Deleting {type(obj).__name__} owned by {obj.tenant_id} as tenant {tid}"
            )


async def apply_rls(session: AsyncSession, tenant_id: str) -> None:
    """On Postgres, bind the tenant for the duration of the transaction (RLS hook)."""
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(
            text("SELECT set_config('app.current_tenant', :tid, true)"),
            {"tid": tenant_id},
        )


class TenantSession:
    """Tenant-scoped facade over an :class:`AsyncSession`.

    All reads are filtered by the active tenant and all writes are validated against it.
    Use this in every request handler instead of the raw session.
    """

    def __init__(self, session: AsyncSession, tenant_id: str):
        self.session = session
        self.tenant_id = tenant_id
        # Bind the tenant to the session itself so the before_flush guard validates against THIS
        # session's tenant, not a process-global that a second concurrent session could overwrite.
        # AsyncSession.info proxies the underlying sync Session.info the guard reads.
        session.info["tenant_id"] = tenant_id

    def add(self, obj: TenantScoped) -> None:
        if obj.tenant_id is None:
            obj.tenant_id = self.tenant_id
        elif obj.tenant_id != self.tenant_id:
            raise TenancyViolation("Cannot add object owned by another tenant")
        self.session.add(obj)

    def add_all(self, objs: Sequence[TenantScoped]) -> None:
        for o in objs:
            self.add(o)

    async def get(self, model: type[ModelT], pk: str) -> ModelT | None:
        obj = await self.session.get(model, pk)
        if obj is None or obj.tenant_id != self.tenant_id:
            return None
        return obj

    def select(self, model: type[ModelT], *where) -> Select:
        """A SELECT already constrained to the active tenant."""
        return select(model).where(model.tenant_id == self.tenant_id, *where)

    async def list(self, model: type[ModelT], *where, limit: int | None = None) -> list[ModelT]:
        stmt = self.select(model, *where)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def first(self, model: type[ModelT], *where) -> ModelT | None:
        return (await self.session.scalars(self.select(model, *where).limit(1))).first()

    async def delete(self, obj: TenantScoped) -> None:
        if obj.tenant_id != self.tenant_id:
            raise TenancyViolation("Cannot delete object owned by another tenant")
        await self.session.delete(obj)

    async def commit(self) -> None:
        await self.session.commit()

    async def flush(self) -> None:
        await self.session.flush()

    async def refresh(self, obj) -> None:
        await self.session.refresh(obj)
