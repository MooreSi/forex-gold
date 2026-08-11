# 002 — ML prob autopsy

## Hypothesis
The engine's stored `ml_prob` (its predicted R-multiple) is anti-predictive:
losers score higher than winners. Test whether that's real as a *ranking*
(H1), whether gating on it helps (H2), and whether retraining on the same
stored features fixes it (H3).

## Data used
`re_signals` from the 21–31 Jul snapshot: 607 win/loss rows, all 741 with
the 24-float `ml_features_json` vector. Labels/P&L come from the **recorded**
book (no simulator involved — this notebook is independent of 001).

## Method
`run.py`: rank AUC of `ml_prob` vs outcome; threshold sweeps (in-sample for
context, then honest walk-forward where each day's rule is chosen only on
prior days); fresh logistic-regression retrain with day-based walk-forward.

## Result — CONFIRMED INVERTED; FEATURES, NOT MODEL, ARE THE PROBLEM  *(2026-08-11)*
- **H1 confirmed**: AUC 0.413 (< 0.5 = actively inverted ranking).
  Spearman vs recorded R: −0.064.
- **H2**: walk-forward gating chose "trade only LOW ml_prob" every fold and
  turned −63.0R (unfiltered test days) into **+4.0R** — but by keeping only
  ~85 of ~430 test-day trades. Read it as "the model is a decent *inverse*
  indicator", not as a tradeable edge on its own.
- **H3**: a fresh logistic model on the same 24 features also fails
  (mean walk-forward AUC 0.465; its top-half picks LOST more than its
  bottom-half: −45.9R vs −17.0R). **The feature vector itself carries an
  inverted signal** — retraining harder won't fix what's being measured.

## Interpretation (for the next notebook)
Something in the feature pipeline correlates with confidence *and* with
losing. Prime suspects, in order: (a) `level_score` — high-scoring
round-number levels were the biggest losers in 001-review; (b) features
derived from the stored `stop_loss` column, which 001 proved is the
*post-hoc* stop, not the original (label leakage in reverse); (c) training
labels poisoned by the upside-down R:R geometry — "win" mostly means "TP1
at 0.43R hit before a 1R stop", so the model learns to love tight-TP
setups that bleed. Backend audit of `ml_engine.extract_features` is
warranted once a lab notebook pins down which feature carries the inversion.

## Verdict
Real, reproducible, and actionable: at minimum the engine should stop using
`ml_prob` as a positive gate (it currently blocks live fills on low scores —
that gate is selecting FOR losers). Next: 003 (feature-by-feature autopsy)
and the R:R geometry grid.
