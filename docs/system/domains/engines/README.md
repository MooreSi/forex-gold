# Engines

**Living file — update when this domain teaches you something.**
Covers: `backend/src/services/test_signal/` (Bounce),
`breakout_signal/`, `reversal_engine/`, `backtest/`.

## What it is

Three independent research engines each generate their own XAUUSD signals,
track them virtually, learn from outcomes, and — only when their own
live-execution toggle is on — place real MT5 orders through the main engine:

- **Bounce / TestSignal** — mean-reversion off key levels
- **Breakout** — trend-following break-and-go / break-and-retest
- **Reversal Engine** — Gold Diggers VIP / GD2 ICT emulation

Each owns an isolated SQLite database, its own adaptive parameters, and its
own ML model, with no cross-training. A separate backtest package replays
recorded candles against the live strategy management rules.

## Where the code lives

- `services/test_signal/test_signal_service.py` — `TestSignalEngine` (Bounce) orchestrator, watchdog self-healing `start()` re-entry
- `services/test_signal/test_signal_generate.py` / `_manage.py` / `_live_execute.py` / `_velocity.py` / `_learn.py` — M15/M5 generation, TP/SL/time-stop management, the one real-order path, 3s velocity monitor, Claude batch tuning every 10 closed trades
- `services/test_signal/signal_generator.py`, `ml_engine.py` (42-feature LightGBM+SGD), `adaptive_params.py`, `claude_reviewer.py`, `market_context.py` (yfinance), `news_filter.py` (Forex Factory), `auth.py`, `database.py`
- `services/breakout_signal/` — same shape: orchestrator, manage/live_execute/velocity/learn, generator, `bo_config` params, 22-feature ML, `bo_`-prefixed store, and `backtest.py` (walk-forward harness)
- `services/reversal_engine/` — orchestrator (levels → pending zone signals → trigger → outcomes → REF correlation), TP1–TP8 ladder management, live execute, `level_detector.py` / `ict_patterns.py` (FVG-iFVG-sweep-breaker "Unicorn"), nightly 22:00 Europe/London Telegram+image research sweep, dual-axis ML, `re_`-prefixed store
- `services/backtest/engine.py` / `simulators.py` / `repo.py` — XAUUSD backtest engine, per-strategy `_simulate_*` walkers, main-DB signal reads

## Constraints / must not change

- **Total isolation between engines**: separate SQLite DBs (`test_signal.db`, `breakout_signal.db`, `reversal_engine.db`), no shared tables or connections, no cross-contamination of ML labels or params.
- Each engine has exactly one real-money surface file (`*_live_execute.py`), gated on its own live-execution toggle. Everything else is virtual tracking with read-only bridge access.
- Adaptive params: every Claude-recommended value is clamped to its `[min, max]` envelope before being applied — "the engine never operates outside the safe envelope."
- Backtest design principles: signals tested only forward from creation time; pre-filtered to the loaded candle window; corrupt signals rejected up front; lot size recomputed on current equity after every trade; commission always deducted.
- `backtest/engine.py`'s Reversal Runner constants must stay in sync with the live `_GDVR_*` values; `simulators.py` is imported lazily to avoid an import cycle.
- `reversal_engine` implements only publicly documented ICT definitions from plain OHLC — no proprietary indicator code.

## Known things & gotchas

- Reversal correlation is **asymmetric on purpose**: our signals fire as price *approaches* a level, the REF channel posts when it *arrives*, so legitimate matches lead by 10–30 minutes. The old symmetric ±300s window failed 498 of 511 matches. `correlation_time_delta_s` is signed: negative = we fired first (good).
- Reversal session is 04:00–16:00 UTC, measured from 591 real REF signals. Asia range is a *level source*, not a trading session. Signal expiry is 2 hours.
- Known bug class in `reversal_engine_manage.py`: `sig["strategy"]` overwritten after `build_signal()` tagged it `"gd2_unicorn"`, so GD2 signals silently fell through to the REF 8-level ladder branch.
- `breakout_signal_repo.py` **deliberately preserves** a known `close_signal` balance double-counting bug (proven by characterization test) — the port's scope was no-behaviour-change.
- Breakout ADX thresholds were rebuilt 2026-07-16 after a ratchet forced every entry into late trends (the 40+ bucket lost $1,258 over 191 trades); floor lowered to 28 go / 24 retest, lateness moved to `max_adx_entry` + `require_adx_rising`.
- `breakout_signal/backtest.py` exists because nightly AI tuning on small recent samples once ratcheted the engine into a losing configuration with no counterfactual check. It omits news windows, spread gate, Claude review and the ML gate — all only *remove* trades, so live selectivity ≥ backtest selectivity.
- Backtest intrabar tie-break is conservative: SL fills before TP within the same M1 bar. Max hold 96 bars, lots clamped 0.01–5.0, $100/point/lot.
- Reversal live execution is blocked when the predicted R-multiple is below 0.
- Three modules read the core DB cross-engine — flagged as inherent coupling preserved as-is.
- All three `panel_data.py` modules transparently swap to mirrored remote stats when the VPS is the active trader.
- `test_signal/auth.py` bakes the hardware fingerprint into the PBKDF2 salt — a password hash from one machine can never verify on another.
- **`test_signal/market_context.py`'s 15-minute cache never worked** (fixed 2026-09-05). `_get_hourly_closes` stored only the last close as a packed float, so the hit branch could not rebuild the list it returns and fell through to a re-fetch every time — the comment on that line admitted it. Every `get_context()` was five live yfinance round trips. Breakout survived it by calling once per signal creation. It now caches the whole `_FETCH_WINDOW`-long list per symbol, so one fetch serves every caller whatever `n` they ask for.
- **Reversal ML v9 (2026-09-05) adds the five macro series** Bounce and Breakout already read — DXY, US10Y, VIX, GVZ, TIP — via `reversal_engine/re_macro.py`. They are **normalised there, not at the call site** as breakout does it, because the Reversal model fits an SGDRegressor alongside LightGBM and SGD is scale-sensitive. `MACRO_NEUTRAL` therefore holds *normalised* values: it is merged into `_FEATURE_NEUTRAL`, which right-pads the stored 33-wide vectors, and raw units there would tell the model the ten-year sat off the top of the scale for every historical signal.
- Macro values are **not `re_signals` columns** — the vector is persisted whole as `ml_features_json`. So a row read back at fill time carries no macro, and `reversal_engine_live_execute` must re-read it or its "same feature set" re-score silently differs from the creation-time vector in five slots. Same trap `rsi14` fell into. Pinned by `tests/reversal_engine/test_macro_call_sites.py`.
- **`reversal_engine/ml_engine.py` is at 799 lines against `LOC_CEILING = 800`** and is not baselined, so the ceiling cannot be raised for it. The v9 additions were held to nine lines by putting the logic and the rationale in `re_macro.py`. The next feature this file gains needs a split first.
- `re_macro.get_cycle_context()` is async and thread-offloaded. The Reversal cycle is 60s and shares its event loop with position management, so a blocking HTTP call in it is not cosmetic.
- **A feature added to an engine cannot be judged by its importance at the retrain that introduces it.** The back-fill gives every historical row the same neutral, so the new column has zero variance and the tree cannot split on it — importance is zero by construction, before any question about the market is asked. Applies to all three engines' `_FEATURE_NEUTRAL` padding, not just Reversal. Pinned with its control in `tests/reversal_engine/test_ml_v9_retrain.py`.

## Open questions

- Database consolidation across engines (QUESTIONS.md #6) — the raw-sqlite3 cross-engine read is "worth a future pack" once revisited.
- Whether the preserved breakout balance double-counting bug should now be fixed.
