# nexus/api/routers/admin_provider_keys.py
"""Provider API keys, managed from the Control plane.

**The key itself is never in a response model** — not even for the superadmin who typed it.
``key_hint`` (its last four characters) is what the UI identifies a row by. There is no endpoint
that returns a stored key, deliberately: a panel that can display credentials is a panel that can
leak them through a screenshot, a support session, or a browser cache.

Gated on ``providers.manage``, which only the ``superadmin`` preset carries. A holder can spend
money through someone else's API key, which is why it is not folded into ``admins.manage``.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from nexus.api.deps import Principal, require_platform_permission
from nexus.billing.audit import record_admin_action
from nexus.billing.permissions import PROVIDERS_MANAGE
from nexus.core.db import get_platform_sessionmaker
from nexus.providers import service
from nexus.providers.catalog import PROVIDERS

router = APIRouter(prefix="/admin/provider-keys", tags=["admin-providers"])


class ProviderKeyOut(BaseModel):
    """Everything the UI needs and nothing it does not. No `key_encrypted`, ever."""

    id: str
    provider: str
    label: str
    key_hint: str
    status: str
    last_depth: str
    last_error: str
    last_error_status: int | None
    enabled: bool
    preferred: bool
    # Which key is actually serving traffic right now. A pool of five keys with no sign of which
    # one is live shows state without showing the state that matters — an operator debugging a
    # provider error needs to know which credential produced it before anything else.
    in_use: bool = False

    @classmethod
    def of(cls, row, *, in_use: bool = False) -> "ProviderKeyOut":
        return cls(
            id=row.id, provider=row.provider, label=row.label, key_hint=row.key_hint,
            status=row.status, last_depth=row.last_depth, last_error=row.last_error,
            last_error_status=row.last_error_status, enabled=row.enabled,
            preferred=row.preferred, in_use=in_use,
        )


class ProviderKeyIn(BaseModel):
    # `forbid` so a body carrying `status` is rejected outright rather than quietly ignored. An
    # admin who could set `verified` by hand could mark a dead key working.
    model_config = {"extra": "forbid"}

    provider: str
    label: str = ""
    key: str = Field(min_length=8)


class LabelIn(BaseModel):
    model_config = {"extra": "forbid"}

    label: str = ""


@router.get("/providers", response_model=list[dict])
async def list_supported_providers(
    _: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> list[dict]:
    """The provider ids the UI may offer. Declared before `/{key_id}` routes so the literal path
    is not swallowed by the parameterised one."""
    from nexus.providers.catalog import MODEL_PROVIDERS

    # `has_model` travels with the id so the UI does not keep its own copy of which providers have
    # one. The same reasoning that put the id list on the server.
    return [{"id": p.id, "label": p.label, "has_model": p.id in MODEL_PROVIDERS}
            for p in PROVIDERS.values()]


@router.get("", response_model=list[ProviderKeyOut])
async def list_provider_keys(
    provider: str = "",
    _: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> list[ProviderKeyOut]:
    rows = await service.list_keys(provider)

    # Computed from the SAME ordering the resolver uses — `preferred` first, then `created_at`,
    # enabled only — so the indicator cannot disagree with which key is really being spent. A
    # separate "which is live" flag on the row would be a second source of truth, and the first
    # thing to drift would be exactly the fact the light exists to report.
    live: dict[str, str] = {}
    for row in sorted(rows, key=lambda r: (not r.preferred, r.created_at)):
        if row.enabled and row.provider not in live:
            live[row.provider] = row.id

    return [ProviderKeyOut.of(r, in_use=(live.get(r.provider) == r.id)) for r in rows]


@router.post("", response_model=ProviderKeyOut, status_code=status.HTTP_201_CREATED)
async def create_provider_key(
    body: ProviderKeyIn,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> ProviderKeyOut:
    try:
        row = await service.add_key(body.provider, body.label, body.key,
                                    user_id=principal.user_id)
    except service.DuplicateKey as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except service.UnknownProvider as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    # The audit records the hint, never the key.
    await _audit(principal, "provider_key.create", row.id,
                 {"provider": row.provider, "hint": row.key_hint})
    return ProviderKeyOut.of(row)


@router.put("/{key_id}/label", response_model=ProviderKeyOut)
async def relabel_provider_key(
    key_id: str,
    body: LabelIn,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> ProviderKeyOut:
    row = await service.update_label(key_id, body.label)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key")
    await _audit(principal, "provider_key.relabel", key_id, {"label": row.label})
    return ProviderKeyOut.of(row)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider_key(
    key_id: str,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> Response:
    # An explicit Response, matching admin_sources: FastAPI refuses to build a response model for
    # a 204, so a `-> None` annotation fails at import time rather than at request time.
    if not await service.delete_key(key_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key")
    await _audit(principal, "provider_key.delete", key_id, {})
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{key_id}/prefer", response_model=ProviderKeyOut)
async def prefer_provider_key(
    key_id: str,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> ProviderKeyOut:
    """Pin this key so it is tried first. Rotation then becomes the failure path, not the norm."""
    row = await service.prefer_key(key_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key")
    await _audit(principal, "provider_key.prefer", key_id, {"provider": row.provider})
    return ProviderKeyOut.of(row)


@router.post("/{key_id}/enabled/{value}", response_model=ProviderKeyOut)
async def set_provider_key_enabled(
    key_id: str,
    value: bool,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> ProviderKeyOut:
    """Disabling is never refused — during an incident "stop using this" must not be blocked."""
    row = await service.set_enabled(key_id, value)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key")
    await _audit(principal, "provider_key.enabled", key_id, {"enabled": value})
    return ProviderKeyOut.of(row)


@router.post("/{key_id}/test", response_model=dict)
async def test_provider_key(
    key_id: str,
    depth: str = "probe",
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> dict:
    """``depth=probe`` proves the credential authenticates and costs nothing meaningful.
    ``depth=verify`` makes a real call through the adapter and costs credits, which is why it is
    never swept and never automatic.

    Both are needed: on 2026-08-21 every Groq key passed the probe and failed every real call,
    because the configured model had been withdrawn.
    """
    from nexus.models.provider_key import ProviderKey
    from nexus.providers.crypto import KeyUnsealable, unseal_key
    from nexus.providers.testing import probe, verify

    if depth not in ("probe", "verify"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unknown depth {depth!r}; expected 'probe' or 'verify'")

    async with get_platform_sessionmaker()() as s:
        row = await s.get(ProviderKey, key_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such key")
        provider = row.provider
        try:
            secret = unseal_key(row.key_encrypted)
        except KeyUnsealable as exc:
            # Distinct from a failed test: the credential was never reached. Marking it `failed`
            # would send an operator to the provider when the problem is on our side.
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    runner = verify if depth == "verify" else probe
    result = await runner(provider, secret)
    await service.mark_tested(key_id, status=result.status, depth=depth,
                              error=result.detail, error_status=result.http_status)
    await _audit(principal, "provider_key.test", key_id,
                 {"depth": depth, "ok": result.ok, "status": result.status})
    return {"ok": result.ok, "status": result.status, "detail": result.detail,
            "http_status": result.http_status}


async def _audit(principal: Principal, action: str, target: str, after: dict) -> None:
    async with get_platform_sessionmaker()() as s:
        await record_admin_action(s, actor=principal.user_id, action=action,
                                  target=target, after=after)
        await s.commit()


# ---- the model, and the live catalogue -------------------------------------------------------------

class ModelIn(BaseModel):
    model_config = {"extra": "forbid"}

    model: str = ""


@router.get("/{provider}/models", response_model=dict)
async def list_provider_models(
    provider: str,
    _: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> dict:
    """What this provider will ACTUALLY accept right now, asked of the provider itself.

    This is the endpoint that would have prevented the 2026-08-21 outage. The configured model,
    `llama-3.3-70b-versatile`, had been withdrawn: every key 404'd, every draft came from the stub,
    and nothing in the product could say why. A hardcoded list would have been just as wrong — the
    catalogue belongs to the provider and changes without notice — so this asks.

    Returns the current choice alongside the options, and never raises: an unreachable provider
    yields an empty list and a reason, which is a different thing from "no models exist".
    """
    from nexus.providers.resolver import model_for
    from nexus.providers.testing import list_models

    if provider not in PROVIDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown provider {provider!r}")

    keys = await service.list_keys(provider)
    usable = [k for k in keys if k.enabled]
    secret = ""
    if usable:
        from nexus.providers.crypto import KeyUnsealable, unseal_key

        try:
            secret = unseal_key(usable[0].key_encrypted)
        except KeyUnsealable:
            secret = ""
    if not secret:
        # Fall back to the environment pool, so the catalogue is listable before anyone has added
        # a managed key — otherwise the first thing an operator wants to see needs a key first.
        from nexus.providers.catalog import env_pool

        env = env_pool(provider)
        secret = env[0] if env else ""

    current = await model_for(provider)
    # Whether the value in force was CHOSEN here or inherited from the environment. Without it the
    # UI cannot say what "Clear" would do — and on a deployment where the env value and the
    # override happen to agree, clearing would look like a no-op right up until someone redeploys
    # with a different NEXUS_GROQ_MODEL and the choice silently changes underneath them.
    overridden = bool(await service.get_model_override(provider))
    if not secret:
        return {"provider": provider, "current": current, "overridden": overridden, "models": [],
                "detail": "no usable key for this provider, so its catalogue cannot be listed"}
    models, detail = await list_models(provider, secret)
    return {"provider": provider, "current": current, "overridden": overridden,
            "models": models, "detail": detail}


@router.put("/{provider}/model", response_model=dict)
async def set_provider_model(
    provider: str,
    body: ModelIn,
    principal: Principal = Depends(require_platform_permission(PROVIDERS_MANAGE)),
) -> dict:
    """Choose the model. Empty clears the override and the environment value applies again.

    Not validated against a fixed list on purpose: the catalogue is the provider's and it changes.
    An operator naming something this deployment has not seen is making a deliberate choice, and
    refusing it would mean a withdrawn-model outage could not be fixed from here — which is the
    exact situation this endpoint exists for.
    """
    try:
        chosen = await service.set_model(provider, body.model, user_id=principal.user_id)
    except service.UnknownProvider as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await _audit(principal, "provider_model.set", provider, {"model": chosen or "(env default)"})
    return {"provider": provider, "model": chosen}
