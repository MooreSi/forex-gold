# 001 — Baseline replay

## Hypothesis
Replaying the 741 recorded signals through `_shared/lib/sim.py` with the
engine's own rules reproduces recorded history (fates and P&L shape). Until
it can, the simulator has no authority over any later experiment.

## Data used
`reversal_engine.db` snapshot (21–31 Jul 2026); ~60-second price series from
`re_analysis_log` (directional precision only, no M1 candles yet).

## Method
`run.py`: replay all settled signals with engine-equivalent config (2h
expiry, 80/10/5/5 TP ladder, break-even after TP1, SL-first tie-break,
zero costs), then compare simulated vs recorded fate per signal, P&L sign
agreement, rank correlation, and the standard metric suite.

## Result — CALIBRATED (directional) ✅  *(2026-08-11)*
- **Win/loss agreement 85.1%** on the 523 signals both settled.
- **P&L sign agreement 84.4%**, Spearman rank corr 0.576.
- Simulated book **−29.9R** vs recorded **≈ −57R**: same direction, ~half
  magnitude — expected, because the baseline models zero spread/commission/
  slippage and the recorded book includes MT5-managed divergences.
- Simulated win rate 67.3% vs recorded ~70%; payoff ratio 0.41 confirms the
  001-review finding (avg loss ≫ avg win) independently of the engine.
- Fill rate 72.8% vs recorded 83.5% — the 60s series misses ~11% of fills
  (price passes through the zone between samples). Signals the sim expires
  but the engine filled are *excluded* from later filter experiments'
  comparisons rather than guessed at.

### Discovery worth keeping (now encoded in sim.py + loaders.py)
`re_signals.stop_loss` stores the **final** stop after break-even/trailing
moves — 359/741 rows have it on the *profit* side of the entry zone. The
original stop must be reconstructed as `zone_mid ∓ sl_dist`. Any analysis
that reads `stop_loss` naively (including, potentially, ML feature
extraction in the backend — worth auditing) is analysing the answer, not
the question.

## Verdict
Good enough to rank ideas (filters, geometry) on. Not good enough to quote
dollar numbers. Re-run this calibration first thing after the MT5 M1 candle
export lands in `_shared/data/`.
