# 003 — Filter stack

## Hypothesis
The three independent findings so far — sub-0.75 R:R signals bleed
(001-review), hours 12–16 & 19 UTC concentrate losses, round-number/
congestion levels lose while asia/swing/unicorn don't — stack into a config
with positive walk-forward expectancy. Also tests replacing the 8-TP ladder
with a single take-profit at 1.0R / 1.5R.

## Data used
All 735 settled signals replayed through `_shared/lib/sim.py` on the
60-second price series (directional precision). Eight configs, all through
the same simulator, zero costs.

## Method
`run.py`: replay each config over the whole period (in-sample, context
only); then walk-forward — pick the best config on prior days, evaluate on
the next unseen day; plus an honesty check with the full stack held *fixed*
over the same test days (no switching). Standard metrics + RESULTS.md.

## Result — FIRST POSITIVE WALK-FORWARD CONFIG  *(2026-08-11)*
- **`stack+tp@1.5R` held fixed on unseen days: +10.0R over 28 trades,
  expectancy +0.357R/trade**, profit factor 1.75 (in-sample), max drawdown
  −2.6R — while the baseline lost −20.3R over 481 trades on the same days.
- Each component alone merely stops the bleeding (baseline −29.9R → −3 to
  −4R in-sample); the *geometry* change (single 1.5R TP) is the only
  component that flips the whole book positive on its own (+9.3R in-sample)
  — more evidence the TP1-heavy ladder is the core defect.
- The adaptive day-by-day config chooser managed only +0.3R: the value is
  in the fixed filters, not in switching. (Good — a fixed rule is also far
  easier to promote and reason about.)
- `tp@1.0R` *without* filters is still negative — geometry alone on bad
  signals in bad hours isn't enough.

## Verdict
KEEP (provisional). Every component was motivated by an earlier finding
rather than searched for, which limits (but does not remove) overfit risk.
28 trades is far too few to promote: **NEEDS-M1** for the geometry leg
(1.5R targets are exactly where 60s sampling flatters fills) and
**NEEDS-MORE-DATA** — re-run on every new snapshot. Nothing goes near the
backend until both.
