"""Capture GTM outcomes and derive per-tenant learned relevance weights.

The loop has three moving parts:

1. **Capture** (:meth:`OutcomeService.record`) — freeze an account's firmographics onto an
   immutable :class:`~nexus.models.outcome.Outcome` row the moment something happens.
2. **Learn** (:meth:`OutcomeService.learned_weights`) — read a tenant's positive outcomes and lean
   the four relevance weights (``industry``/``size``/``geo``/``tech``) toward the firmographic axes
   those wins actually share, overriding the static :data:`DEFAULT_WEIGHTS`.
3. **Apply** — :class:`~nexus.relevance.engine.RelevanceEngine.score_icp_fit` accepts the learned
   weights and blends them in (see the lookalike scoring path).

The learning here is the **deterministic seed** the spec calls for ("interface now, implement
later"): a small, explainable heuristic that runs synchronously with zero external dependencies, so
it is fully testable offline. The later nightly job will replace :meth:`_compute_weights` with a
proper correlation/regression model without changing this interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select

from nexus.core.tenancy import TenantSession
from nexus.models.account import Account
from nexus.models.outcome import Outcome
from nexus.relevance.engine import DEFAULT_WEIGHTS

# The funnel stages we capture, ordered. ``lost`` is the explicit negative terminal.
STAGES: tuple[str, ...] = ("sent", "replied", "meeting", "won", "lost")

# Stages that count as positive reinforcement, weighted by how much signal each carries.
STAGE_IMPACT: dict[str, int] = {"replied": 1, "meeting": 2, "won": 4}
POSITIVE_STAGES: frozenset[str] = frozenset(STAGE_IMPACT)

# Minimum total positive impact before we trust learned weights over the static defaults. Below
# this, ``learned_weights`` returns ``None`` and callers fall back to ``DEFAULT_WEIGHTS``.
MIN_SIGNAL = 5

# How far learned weights may pull away from the static defaults (0 = ignore data, 1 = trust fully).
# Half-and-half regularizes against small, noisy samples.
LEARN_RATE = 0.5

# The firmographic axes, paired with the predicate that decides whether an outcome carries signal
# for that axis (i.e. the datum was known on the won account).
_AXES: tuple[str, ...] = ("industry", "size", "geo", "tech")


@dataclass(slots=True)
class LearnedWeights:
    """A tenant's relevance weights, learned or defaulted, with the evidence behind them."""

    weights: dict[str, float]
    learned: bool
    sample_size: int  # number of positive outcomes the weights were derived from
    defaults: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    def as_dict(self) -> dict:
        return {
            "weights": dict(self.weights),
            "learned": self.learned,
            "sample_size": self.sample_size,
            "defaults": dict(self.defaults),
        }


def _axis_present(o: Outcome) -> dict[str, bool]:
    """Which firmographic axes were known on the account when the outcome was captured."""
    return {
        "industry": bool(o.industry),
        "size": o.employee_count is not None,
        "geo": bool(o.country),
        "tech": (o.tech_count or 0) > 0,
    }


class OutcomeService:
    async def record(
        self,
        ts: TenantSession,
        *,
        stage: str,
        account: Account | None = None,
        account_id: str | None = None,
        contact_id: str | None = None,
        campaign_id: str | None = None,
        meta: dict | None = None,
    ) -> Outcome:
        """Capture one outcome, freezing the account's firmographics onto the row.

        Pass an ``account`` to snapshot its firmographics; ``account_id``/``contact_id`` link the
        row without requiring the loaded object. ``campaign_id`` attributes the outcome to the
        campaign that drove it (feeds the per-campaign ROI rollup). ``stage`` must be one of
        :data:`STAGES`.
        """
        if stage not in STAGES:
            raise ValueError(f"Unknown outcome stage {stage!r}; expected one of {STAGES}")
        outcome = Outcome(
            tenant_id=ts.tenant_id,
            stage=stage,
            account_id=account.id if account is not None else account_id,
            contact_id=contact_id,
            campaign_id=campaign_id,
            industry=account.industry if account is not None else None,
            employee_count=account.employee_count if account is not None else None,
            country=account.country if account is not None else None,
            tech_count=len(account.tech_stack or []) if account is not None else 0,
            meta=meta or {},
        )
        ts.add(outcome)
        await ts.flush()
        return outcome

    async def campaign_attribution(self, ts: TenantSession, campaign_id: str) -> dict[str, int]:
        """Per-stage outcome counts attributed to one campaign — the ROI rollup managers read."""
        rows = (
            await ts.session.execute(
                select(Outcome.stage, func.count())
                .where(
                    Outcome.tenant_id == ts.tenant_id,
                    Outcome.campaign_id == campaign_id,
                )
                .group_by(Outcome.stage)
            )
        ).all()
        return {stage: int(n) for stage, n in rows}

    async def list_recent(self, ts: TenantSession, *, limit: int = 50) -> list[Outcome]:
        stmt = (
            ts.select(Outcome)
            .order_by(Outcome.created_at.desc())
            .limit(max(1, min(limit, 200)))
        )
        return list((await ts.session.scalars(stmt)).all())

    async def summary(self, ts: TenantSession) -> dict:
        """Per-stage counts for manager attribution dashboards."""
        stmt = (
            select(Outcome.stage, func.count())
            .where(Outcome.tenant_id == ts.tenant_id)
            .group_by(Outcome.stage)
        )
        rows = (await ts.session.execute(stmt)).all()
        counts = {stage: int(n) for stage, n in rows}
        return {
            "total": sum(counts.values()),
            "by_stage": {stage: counts.get(stage, 0) for stage in STAGES},
            "positive": sum(counts.get(s, 0) for s in POSITIVE_STAGES),
        }

    async def learned_weights(self, ts: TenantSession) -> LearnedWeights:
        """Derive this tenant's relevance weights from its positive outcomes.

        Returns learned weights when there is enough signal (≥ :data:`MIN_SIGNAL` total impact),
        otherwise the static defaults flagged ``learned=False`` so callers can show "still
        learning" and the engine scores exactly as before.
        """
        positives = await ts.list(
            Outcome, Outcome.stage.in_(tuple(POSITIVE_STAGES))
        )
        total_impact = sum(STAGE_IMPACT[o.stage] for o in positives)
        if total_impact < MIN_SIGNAL:
            return LearnedWeights(
                weights=dict(DEFAULT_WEIGHTS), learned=False, sample_size=len(positives)
            )
        weights = self._compute_weights(positives)
        if weights is None:
            return LearnedWeights(
                weights=dict(DEFAULT_WEIGHTS), learned=False, sample_size=len(positives)
            )
        return LearnedWeights(weights=weights, learned=True, sample_size=len(positives))

    def _compute_weights(self, positives: list[Outcome]) -> dict[str, float] | None:
        """The seed heuristic: lean weights toward axes that winning accounts share.

        Accumulate each positive outcome's impact onto every axis that was *known* on the account,
        normalize to a distribution, blend with the static defaults (regularizing small samples via
        :data:`LEARN_RATE`), and renormalize so the four weights sum to 1.
        """
        accrued = dict.fromkeys(_AXES, 0.0)
        for o in positives:
            impact = STAGE_IMPACT[o.stage]
            for axis, present in _axis_present(o).items():
                if present:
                    accrued[axis] += impact
        total = sum(accrued.values())
        if total <= 0:
            return None
        learned_share = {axis: accrued[axis] / total for axis in _AXES}
        blended = {
            axis: (1 - LEARN_RATE) * DEFAULT_WEIGHTS[axis] + LEARN_RATE * learned_share[axis]
            for axis in _AXES
        }
        norm = sum(blended.values()) or 1.0
        return {axis: round(blended[axis] / norm, 4) for axis in _AXES}


_service: OutcomeService | None = None


def get_outcome_service() -> OutcomeService:
    global _service
    if _service is None:
        _service = OutcomeService()
    return _service


def set_outcome_service(service: OutcomeService | None) -> None:
    global _service
    _service = service
