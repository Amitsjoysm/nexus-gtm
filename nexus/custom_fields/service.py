"""Custom proprietary-data fields: a column catalog + CSV upsert onto Account/Contact.

Reps bring columns NEXUS has no native schema for. Values live in the per-row
``custom_fields`` JSON; each column's metadata lives in ``CustomFieldDef`` so tables can
render and filter it. Import matches rows by a natural key — account ``domain`` or contact
``email`` — and never creates new entities (unmatched rows are skipped)."""
from __future__ import annotations

import csv
import io
import re

from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account, Contact
from nexus.models.chat import (
    CF_KINDS,
    ENTITY_ACCOUNT,
    ENTITY_CONTACT,
    CustomFieldDef,
)

_KEY_RE = re.compile(r"[^a-z0-9_]+")


def _slug(label: str) -> str:
    return _KEY_RE.sub("_", (label or "").strip().lower()).strip("_")[:60] or "field"


class CustomFieldError(ValueError):
    """Bad input the router surfaces as 400."""


class CustomFieldService:
    async def list_defs(
        self, ts: TenantSession, entity: str | None = None
    ) -> list[CustomFieldDef]:
        where = () if entity is None else (CustomFieldDef.entity == entity,)
        return await ts.list(CustomFieldDef, *where)

    async def create_def(
        self, ts: TenantSession, *, entity: str, key: str | None, label: str, kind: str = "text"
    ) -> CustomFieldDef:
        entity = (entity or "").strip().lower()
        if entity not in (ENTITY_ACCOUNT, ENTITY_CONTACT):
            raise CustomFieldError(f"unknown entity {entity!r}")
        if kind not in CF_KINDS:
            raise CustomFieldError(f"unknown kind {kind!r}")
        slug = _slug(key or label)
        existing = await self._get_def(ts, entity, slug)
        if existing is not None:
            return existing
        d = CustomFieldDef(entity=entity, key=slug, label=label or slug, kind=kind)
        ts.add(d)
        await ts.flush()
        return d

    async def delete_def(self, ts: TenantSession, def_id: str) -> bool:
        d = await ts.get(CustomFieldDef, def_id)
        if d is None:
            return False
        await ts.delete(d)
        await ts.flush()
        return True

    async def _get_def(
        self, ts: TenantSession, entity: str, key: str
    ) -> CustomFieldDef | None:
        return await ts.first(
            CustomFieldDef, CustomFieldDef.entity == entity, CustomFieldDef.key == key
        )

    async def import_csv(
        self,
        ts: TenantSession,
        *,
        entity: str,
        content: bytes,
        match_column: str,
        mapping: dict[str, str],
    ) -> dict:
        entity = (entity or "").strip().lower()
        if entity not in (ENTITY_ACCOUNT, ENTITY_CONTACT):
            raise CustomFieldError(f"unknown entity {entity!r}")
        if not match_column:
            raise CustomFieldError("match_column is required")

        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        headers = reader.fieldnames or []
        if match_column not in headers:
            raise CustomFieldError(f"match_column {match_column!r} not in CSV header")

        # {csv_column: field_key}, keeping only columns actually present in the file.
        cols = {c: _slug(k) for c, k in (mapping or {}).items() if c in headers}
        if not cols:
            raise CustomFieldError("no mapped columns found in CSV header")

        # Auto-create any missing CustomFieldDef (label defaults to the CSV column name).
        created_fields: list[str] = []
        for csv_col, key in cols.items():
            if await self._get_def(ts, entity, key) is None:
                ts.add(CustomFieldDef(entity=entity, key=key, label=csv_col, kind="text"))
                created_fields.append(key)
        await ts.flush()

        matched = updated = skipped = 0
        for row in reader:
            raw = (row.get(match_column) or "").strip().lower()
            if not raw:
                skipped += 1
                continue
            target = await self._match(ts, entity, raw)
            if target is None:
                skipped += 1
                continue
            matched += 1
            cf = dict(target.custom_fields or {})
            changed = False
            for csv_col, key in cols.items():
                val = (row.get(csv_col) or "").strip()
                if val == "":
                    continue
                if cf.get(key) != val:
                    cf[key] = val
                    changed = True
            if changed:
                target.custom_fields = cf
                flag_modified(target, "custom_fields")
                updated += 1
        await ts.flush()
        return {
            "matched": matched,
            "updated": updated,
            "created_fields": created_fields,
            "skipped": skipped,
        }

    async def _match(self, ts: TenantSession, entity: str, raw: str):
        if entity == ENTITY_ACCOUNT:
            return await ts.first(Account, func.lower(Account.domain) == raw)
        return await ts.first(Contact, func.lower(Contact.email) == raw)
