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
