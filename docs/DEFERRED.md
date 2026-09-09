# Deferred — decided, not yet built

Things consciously postponed, with the reasoning, so a later session does not have to re-derive it
or accidentally reverse a decision that was made deliberately.

---

## Expose region and postal weights in Fit weighting

**Decided 2026-09-04: keep them fixed for now, revisit later.**

`regions` (state/province) and `postal_codes` (zip) are already scored by
`nexus/relevance/engine.py` and already settable in the Relevance UI. What is NOT adjustable is
their **weight**: both are pinned at `0.075`, half of everything else, while `industry` (0.35),
`size` (0.30), `geo` (0.15) and `tech` (0.20) can be moved with the Fit weighting sliders.

The half weight is deliberate. From the code comment:

> Region and postal are deliberately HALF weight: they refine a geography `geo` already scores,
> and at full weight a single ICP could let location outvote industry and size together.

**Why it might still be worth exposing:** a business whose ICP genuinely IS geographic — field
sales, local services, a franchise territory — cannot express that today. They can name the
postcodes; they cannot make them matter.

**Why it was not done now:** an ICP where location outvotes industry and size makes the fit score
mean something different in that workspace than everywhere else, and the score is compared across
workspaces in the admin console. If it is exposed, the likely right shape is a **ceiling** — let
the combined location weight rise to perhaps 0.35 but no further — which needs explaining in the UI
or the slider stopping short reads as a bug.

**Ask before building.** This is a product decision about what a fit score means, not a gap.

Related, already shipped: `_within_geo` in `nexus/discovery/auto.py` makes a COUNTRY outside the
ICP a hard non-match at discovery, so the common "why am I seeing UK companies" case no longer
depends on weighting at all.

---

## Plays do not fire for shared-crawl-covered accounts

**Found 2026-09-09 by triggering a play from the UI. Logged for a later decision; nothing changed.**

`PlaysEngine.evaluate` is called from exactly one place: `pipeline.process_account`, over the
`new_signals` that run produced, with the freshly computed `composite` passed in.

An account covered by the shared company crawl (`companies.crawl_verdict = "agrees"`) **skips the
per-tenant crawl**, so `new_signals` is empty on every `process_account` run after its first, and
its signals arrive instead from the scheduled fan-out sweep — which calls
`IngestionService.ingest` directly and never returns to the pipeline.

The first run is fine: `process_account` backfills from the shared crawl when
`last_refreshed_at is None` and re-reads the delivered signals precisely so "scoring, inbox and
plays below see them". It is every run after that where the gap opens.

**Alerts are unaffected**, because `signal_alerts` subscribes to `signal.created` on the event bus.
That is what makes this invisible: the rep still gets the alert, so nothing looks broken, and only
the automation silently stops.

Blast radius on 2026-09-09: **one** company, the only one at `crawl_verdict = "agrees"`. It grows by
one for each of the 107 companies awaiting a verdict that an operator approves.

### The trade-off, which is why this was not just fixed

`signal.created` is published during ingest (`pipeline.py:116`); scoring runs afterwards
(`pipeline.py:155`). So a plays subscriber on the bus fires **before the account is rescored** and
reads the PREVIOUS composite.

Concretely: a play gated on `min_composite: 60`, on an account moving 55 -> 70 *because of the
funding signal that just landed*. It fires today. On the bus it would not.

Three shapes were considered:

1. **Narrow** — fan-out evaluates plays, mirroring the alerting it already inherits. No behaviour
   change anywhere else. Cost: a third ingest path added later reopens the same hole, and two call
   sites must stay in agreement — a drift this codebase has already had once, between
   `fanout_company` and `pipeline._covered_by_shared_crawl`, and pinned by a test for that reason.
2. **Structural** — plays subscribe to `signal.created`. Covers every path, present and future.
   Cost: the `min_composite` semantics above, plus the pipeline call must be removed in the same
   change or every pipeline signal fires its plays twice.
3. **Bus subscriber that reads the persisted score first.** Keeps `min_composite` correct *and*
   covers every path, at the cost of a score lookup per signal — the N+1 the pipeline deliberately
   avoids by loading the enabled plays once for the whole batch.

### Also to fix whenever this is picked up

`nexus/core/events.py` opens with:

> "Decouples subsystems: ingestion emits `signal.created`, the plays engine and inbox subscribe."

The plays engine does **not** subscribe. Only alerts do. A docstring describing a wiring that does
not exist is how this stayed invisible — anyone reading it would reasonably conclude that a
fan-out signal triggers plays. Correct it to describe reality, or make it true, depending on which
shape above is chosen.
