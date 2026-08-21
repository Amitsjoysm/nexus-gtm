"""Plays engine: evaluate signal→action automations and execute matching actions.

A Play has a declarative trigger and a list of actions. When a signal arrives (with the account's
current composite score), every enabled play whose trigger matches fires its actions:
``create_task`` (inbox), ``alert`` (notification), ``sep_push`` (Outreach/Salesloft),
``crm_push`` (write the enriched account + contacts back to the CRM).
"""
from __future__ import annotations

from nexus.alerts.service import get_alert_service
from nexus.core.tenancy import TenantSession
from nexus.inbox.service import get_inbox_service
from nexus.ingestion import crm_credentials
from nexus.integrations.sep import get_sep_connector
from nexus.models.account import Account, Contact
from nexus.models.signal import SignalEvent
from nexus.models.workflow import Play, PlayRun


def _trigger_matches(trigger: dict, signal: SignalEvent, composite: int | None) -> bool:
    kinds = trigger.get("signal_kinds")
    if kinds and signal.kind not in kinds:
        return False
    if signal.strength < trigger.get("min_strength", 0.0):
        return False
    if trigger.get("min_composite") is not None:
        if composite is None or composite < trigger["min_composite"]:
            return False
    return True


class PlaysEngine:
    async def evaluate(
        self,
        ts: TenantSession,
        *,
        account: Account,
        signal: SignalEvent,
        composite: int | None = None,
        plays: list[Play] | None = None,
    ) -> list[PlayRun]:
        # Callers in a per-signal loop should preload the enabled plays once and pass
        # them in; standalone callers fall back to self-loading for convenience.
        if plays is None:
            plays = await ts.list(Play, Play.enabled == True)  # noqa: E712
        runs: list[PlayRun] = []
        for play in plays:
            if not _trigger_matches(play.trigger or {}, signal, composite):
                continue
            detail = await self._execute_actions(ts, play, account, signal, composite)
            run = PlayRun(
                tenant_id=ts.tenant_id,
                play_id=play.id,
                account_id=account.id,
                signal_id=signal.id,
                status="executed",
                detail=detail,
            )
            ts.add(run)
            runs.append(run)
        if runs:
            await ts.flush()
        return runs

    async def _execute_actions(
        self, ts, play: Play, account: Account, signal: SignalEvent, composite: int | None
    ) -> dict:
        outcomes: list[dict] = []
        for action in play.actions or []:
            atype = action.get("type")
            if atype == "create_task":
                task = await get_inbox_service().create_from_signal(
                    ts, signal, account,
                    composite_score=composite,
                    owner_user_id=action.get("owner_user_id"),
                    suggested_action={"type": "play", "play": play.name},
                )
                outcomes.append({"type": "create_task", "task_id": task.id})
            elif atype == "alert":
                # Enrich the alert with actionable intelligence (category, importance, matched-ICP,
                # suggested + next-best action, source URL). Merged into meta, which AlertOut
                # already exposes — so no schema change and no API break. Best-effort: a failure
                # here must never block the alert from being raised.
                from nexus.alerts.enrichment import build_alert_intelligence

                meta = {"play": play.name}
                try:
                    meta.update(build_alert_intelligence(account, signal, composite))
                except Exception:  # intelligence is additive; never fail the alert on it
                    pass
                alert = await get_alert_service().create(
                    ts,
                    title=action.get("message") or signal.title,
                    body=action.get("body", ""),
                    severity=action.get("severity", "info"),
                    channel=action.get("channel", "in_app"),
                    account_id=account.id,
                    signal_id=signal.id,
                    source="play",
                    meta=meta,
                )
                outcomes.append(
                    {"type": "alert", "alert_id": alert.id, "channel": alert.channel}
                )
            elif atype == "sep_push":
                contact = await ts.first(Contact, Contact.account_id == account.id)
                res = await get_sep_connector().push_contact(
                    sequence=action.get("sequence", "default"),
                    email=contact.email if contact else None,
                    payload={"account": account.name, "signal": signal.title},
                )
                outcomes.append({"type": "sep_push", "ok": res.ok, "platform": res.platform})
            elif atype == "crm_push":
                contacts = await ts.list(Contact, Contact.account_id == account.id)
                # Resolve once and reuse for both calls — this tenant's CRM, not the process's.
                connector = await crm_credentials.resolve_crm_connector(ts)
                res = await connector.push_account(account, contacts=contacts)
                outcomes.append(
                    {"type": "crm_push", "ok": res.ok, "source": res.source}
                )
                if action.get("log_activity", True):
                    await connector.push_activity(
                        account_id=account.crm_id or account.id,
                        kind="signal",
                        detail={"signal": signal.title, "play": play.name},
                    )
        return {"play": play.name, "actions": outcomes}


_engine = PlaysEngine()


def get_plays_engine() -> PlaysEngine:
    return _engine
