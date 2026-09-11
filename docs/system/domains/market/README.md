# Market structure

**Living file -- update when this domain teaches you something.**
Covers: `backend/src/services/market/`.

## What it is

Pure market-structure primitives shared by every engine. Nothing here
reaches the database or the broker: these are functions over candles and
ticks, which is what makes them equally usable live, in a backtest, and in
an offline study.

The domain was created on 2026-09-11 building
[docs/todo/reversal-engine/200](../../../todo/reversal-engine/200-what-a-professional-desk-would-add.md).
Before it, the only structural analysis in the app lived inside one
engine's `level_detector` and `ict_patterns`, so nothing else could use it
and nothing could be measured against anything else.

## Where the code lives

- `price_path.py` -- a trade's real path as `(ts, high, low)` points, from ticks or bars, plus excursion
- `exit_replay.py` -- what a different exit rule would have returned on that path
- `barrier_fit.py` -- stop and target fitted to the excursion distribution; expectancy sweeps with bootstrap intervals and a chronological holdout
- `liquidity_map.py` -- previous day/week levels, daily and weekly opens, initial balance, session VWAP, POC and value area
- `vwap.py`, `volume_profile.py` -- the two reference-price calculations behind those levels
- `entry_trigger.py` -- rejection, deceleration, sweep-and-reclaim confirmation at a level
- `order_flow.py` -- feed capability probe, cumulative delta (measured or tick-rule), spread dynamics
- `correlation.py` -- cross-asset correlation and a correlation-aware exposure number
- `validation.py` -- purged/embargoed k-fold, walk-forward, deflated Sharpe, probability of backtest overfitting, uniqueness weights

## Constraints

- **Pure.** No DB, no broker, no clock. A function here that needed `time.time()` would be untestable against history, which is the entire point of the domain.
- **A missing measurement is `None`, never `0.0`.** Excursion with no tick coverage, correlation of a flat series, cost with no tick to price it: each returns None. Zero is a claim, and every average downstream would silently absorb it.
- **Side of the book matters.** A long is closed at the bid and a short at the ask. `price_path` walks the correct side; using the mid understates every trade's adverse excursion by half the spread, in the same direction every time.
- **Bars are ambiguous, ticks are not.** `is_ambiguous` marks a bar point; `exit_replay` resolves those stop-first, matching `backtest/template_simulator`.

## Known things

- **The broker's tick stream carries no trade side, probably.** `mt5_bridge` requests `COPY_TICKS_ALL` and, since 2026-09-11, passes `flags`, `last` and `volume` through. Whether this Vantage feed actually populates them is unmeasured -- `order_flow.probe_feed` answers it in one call, and nothing should be built on delta until it has.
- **Tick volume is a count of quote changes, not size.** Volume profile and VWAP say so through `volume_is_proxy`.
- **PBO comes out high on noise, not at 0.5.** In a finite sample the in-sample and out-of-sample halves partition the same draws, so a configuration that won in sample by luck gives that luck back on the complement. Measured at 0.85 on a 20-configuration noise fixture. Read the direction, not the digit.

## Open questions

- The liquidity-map level types (`pdh`, `pwl`, `vwap`, `poc`, …) carry a provisional strength of 2 and inherit `score_level`'s 0.50 default for unknown types. Their real weights need the per-type attribution in `reversal_engine/attribution.py` over enough closed trades, and should not be raised before that.
- `correlation.effective_exposure` assumes an unmeasured pair is perfectly correlated. That is deliberately conservative and it has never been calibrated against this account's actual multi-instrument behaviour, because the account has never held one.
