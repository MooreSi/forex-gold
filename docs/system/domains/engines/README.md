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

- **The Reversal Engine ML version history, and why a bump used to be dangerous (moved here from `ml_engine.py` 2026-09-08).** That file sits on the 800-line ceiling, and this is rationale rather than code. Each bump discards the fitted models because a changed feature width or label makes the old ones invalid; **the training DATA is never lost** — `_collect_training_data` re-reads every closed signal from the database and right-pads older rows with `_FEATURE_NEUTRAL`. What a bump used to cost was the model itself until the next retrain, and that window was dangerous because **the ML gate fails OPEN**: `reversal_engine_live_execute` blocks only `if fresh_prob is not None and < 0`, and `predict()` returns None with no model, so every signal executed unfiltered. v9 shipped Saturday 2026-09-05 and the first v9 retrain was Monday 09:27. Since 2026-09-08 `ml_handover.py` hands the previous model over instead — it keeps scoring the leading features it was fitted on (valid because features are append-only) until a retrain replaces it, and **only from v5**, because v5 replaced the label and an older model predicts a different quantity that the gate would compare to zero. The per-version history:

```
  v3 switches to R-multiple regression and adds 4 new features (news_proximity_norm,
  regime_score, equity_drawdown_pct, concurrent_agreement) — discards v2 models so
  dimension and label format mismatches can't happen; retrains from scratch.
  v4 adds ref_discipline_score/ref_aggression_score — daily values derived by
  telegram_research.py's nightly AI read of the reference channel/GD2 messages+images, cached
  in re_config and refreshed once per night. Same discard-and-retrain-from-
  scratch handling as v3 for the same reason (dimension mismatch).
  v5 (2026-07-31) keeps v4's features but replaces the LABEL: was
  `rr_tp1 if win else -1.0`, now realised net R (see _realised_r). The old
  label was a fiction -- it priced every loss at exactly -1.0R and every win
  at its planned rr_tp1, so summed over the 576 closed signals it read +51.1
  ("profitable") while the same trades actually lost $2,691 (sum of realised
  R: -46.1). Measured against real rows: losses averaged -1.22R (worst
  -5.75R, stops slipping well past sl_dist) and wins +0.39R, a true payoff of
  0.32:1 versus the 0.54:1 the model was being told. Retrained from scratch
  because a model fitted on the old label is calibrated to the wrong scale.
  v8 (2026-08-06) appends `pro_likeness` -- the output of pro_model.py, a
  classifier trained on "a reference channel fired here" vs "background", so
  what the professionals do enters this model as ONE weighted opinion rather
  than as training rows of its own (their signals have no realised R of ours
  to regress against, and pooling them would answer a different question with
  the same weights). Same discard-and-retrain handling as v3-v7: the stored
  vectors are back-filled to the new width by _FEATURE_NEUTRAL, so the
  training history survives even though the fitted models do not.
  v9 (2026-09-05) appends the five macro series Bounce and Breakout already
  read -- DXY, US10Y, VIX, GVZ, TIP -- normalised in re_macro.py (which says
  why there). Discard-and-retrain as v3-v8. Spec: docs/todo/001-reversal-macro-context.md.
```
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
- **`reversal_engine/ml_engine` is a package as of 2026-09-11**, `__init__.py` 635 lines against `LOC_CEILING = 800`. It reached 785 and is not baselined, so the ceiling could not be raised for it; the fleet-model work (reversal-engine/070) edits `predict()` and had nowhere to go. **Only two seams were taken, and the reason the rest were not is the important part:** `__init__` rebinds six module globals — `_model_batch`, `_model_online`, `_labeled_count`, `_ref_level_stats`, `_train_history`, `_data_dir` — across thirteen functions, and splitting a module that rebinds a global forks that state (rules/70 §5). What moved is what touches none of it: `_feature_schema.py` (the names and neutrals, needed by two importers) and `_training_data.py` (the label and the training set). `extract_features` stayed, because it sits with the state. Everything is re-exported, so `ml_engine.FEATURE_NAMES` and `ml_engine._realised_r` still resolve and every existing test passed unmodified.
- `re_macro.get_cycle_context()` is async and thread-offloaded. The Reversal cycle is 60s and shares its event loop with position management, so a blocking HTTP call in it is not cosmetic.
- **A feature added to an engine cannot be judged by its importance at the retrain that introduces it.** The back-fill gives every historical row the same neutral, so the new column has zero variance and the tree cannot split on it — importance is zero by construction, before any question about the market is asked. Applies to all three engines' `_FEATURE_NEUTRAL` padding, not just Reversal. Pinned with its control in `tests/reversal_engine/test_ml_v9_retrain.py`. **What tells you when it IS readable is a count, not a date (2026-09-11):** the stored vectors whose tail differs from `_FEATURE_NEUTRAL`. For v9 macro that reached **632 of 5,293 labelled rows (12%)** six days after the bump, at which point `dxy_momentum` ranks **7 of 38** by both split and gain — so the "all five land in the bottom quartile" outcome did not happen. Read that table knowing three things: importance is not predictive value (`level_score` is this engine's standing counter-example), a continuous feature attracts splits by cardinality alone, and 88% of the rows are still the neutral constant. `docs/todo/001-reversal-macro-context.md` §9 carries the numbers.
- **The ML gate fails open when there is no model at all** — a fresh install, or a handover refused across the label epoch (below v5). `reversal_engine_live_execute` blocks only `if fresh_prob is not None and < 0`, and `fresh_prob` starts as the creation-time `ml_prob`, which is None when nothing was fitted. `ml_handover` closed the *version-bump* window, which is narrower than "the gate no longer fails open" — a phrase used in the 2026-09-09 session note and worth not repeating.

## Open questions

- Database consolidation across engines (QUESTIONS.md #6) — the raw-sqlite3 cross-engine read is "worth a future pack" once revisited.
- Whether the preserved breakout balance double-counting bug should now be fixed.

## Reversal engine: the 2026-09-11 capability build

`docs/todo/reversal-engine/210` is the inventory. The things worth knowing
here rather than there:

- **The engine no longer copies Gold Diggers** (owner, 2026-09-11 --
  `docs/simon-handover/029`). `signal_generator`'s fixed TP cascade and
  level-score stop are the channel's geometry; `atr_barriers` replaces both
  with volatility multiples when `re_atr_barriers_enabled` is on. It is off.
  `score_level`'s type weights are still calibrated against the channel's
  hit rate and were deliberately left alone.
- **`use_dynamic_atr` is implemented in Python, not the EA.**
  `trading/template_levels.template_sl_at` sizes the stop and
  `open_trade.resolve_template_tps` sizes TP1. The EA reads resolved prices
  and knows nothing about ATR. A grep of `ForexTraderBridge.mq5` for "ATR"
  finds only the display panel, which reads like a missing feature and is
  not one.
- **`cycle_setup.py` holds the per-cycle context and the extra levels**, not
  the service. The service was five lines under its 800-line ceiling.
- **The new gates all live behind `risk/capability_gates.py`**, which is the
  one place the switches are read. A settings row missing those columns
  entirely behaves exactly as it did.
- **The live path records a shadow decision for five variants on every fill
  attempt**, from facts it has already computed. The liquidity gate moved to
  sit beside the other two new gates so all three are evaluated before any
  of them returns -- otherwise a challenger gets recorded as "would take" on
  a signal whose later gates were never run.

## The reporting epoch (2026-09-11)

`reversal_engine/stats_repo.stats_epoch()` is a timestamp; every number on
the Reversal Engine panel ignores anything closed before it, and
`reset_stats()` moves it to now and puts the virtual balance back to
$1,000. **It deletes nothing.**

Two consequences worth knowing before touching either side:

- `reconcile_balance_with_trades()` is epoch-filtered, because it runs on
  every `init()` and would otherwise silently restore the pre-reset balance
  at the next restart -- the reset would look as though it had never
  happened.
- `get_recent_win_rate()` is deliberately NOT filtered. It is a feature in
  the model'''s vector, not a number on a panel, and a reporting reset must
  not quietly change what the model is told about the market. Anything else
  added to the panel should follow the same split: reporting reads the
  epoch, features do not.


## The Bounce engine's three counter-trend gates (2026-09-12)

`test_signal_generate.generate` refuses a trigger it has already found in three
places, and until now all three were inline boolean expressions inside a method
that cannot be called without candles, a bridge and a database. Nothing tested
them; the package sits at 25.6% coverage. They are now
`services/test_signal/_gates.py` — `extreme_trend_blocks`, `dual_bias_blocks`,
`asian_counter_bias_blocks` — each returning the reason it refused or None,
which is the shape `risk/governor.htf_bias_blocks` and `risk/capability_gates`
already use.

**Behaviour is unchanged and that is a fact, not a reading.**
`tests/test_signal/test_generate_gates.py` evaluates the original expressions,
copied verbatim from before the move, against the extracted functions over
every combination of their inputs (150 for the Asian gate, 720 each for the
other two). Four mutants killed. A fifth survives and is equivalent: the
`htf_bias == "neutral"` guard in the Asian gate is redundant against
`_is_counter_bias`, kept because the original carried it, and flagged in a
comment so nobody re-derives that.

The three are easy to confuse and differ in ways that matter:

| | fires on | direction | a liquidity sweep |
|---|---|---|---|
| `extreme_trend_blocks` | H1+H4 agree, ADX >= tunable | ignored | **refused too** |
| `dual_bias_blocks` | H1+H4 agree, ADX >= tunable | counter only | exempt |
| `asian_counter_bias_blocks` | the Asian session | counter only | exempt |

The sweep asymmetry is deliberate: a sweep's premise is a level holding against
the crowd, and the extreme-trend gate exists precisely because levels stop
holding in a persistent trend.

**The open question, now visible.** `asian_counter_bias_blocks` refuses
counter-bias signals in this engine's Asian session and has done since before
anything was measured. Note the two engines do not agree on when that is:
`test_signal/signal_generator.get_session` calls **23:00-07:59** Asian, and
`reversal_engine/level_detector.get_session` calls it **00:00-07:59**. The
23:00 hour is in one engine's rule and outside the other's. The Reversal Engine's own numbers over the same hours
say the opposite — see the risk domain README and
`capability_gates.asian_bias_exempt`. The two engines trade different setups,
so it is possible both are right; nothing on this engine was changed on the
strength of the other's data. It is the owner's call:
`docs/simon-handover/033`.

## The engines are not as isolated as the top of this file says (2026-09-12)

> *"Each owns an isolated SQLite database, its own adaptive parameters, and its
> own ML model, with no cross-training."*

The databases and the models are isolated. **The adaptive parameters are not.**

`breakout_signal/signal_generator.py` re-exports `session_quality` and
`session_is_active` from `test_signal/signal_generator.py`, and
`session_quality` reads `ap.get("allow_asian")` — a **Bounce** parameter, held
in the **Bounce** database, written by the **Bounce** engine's Claude tuner
from **Bounce** trade outcomes. The only live caller is
`breakout_signal_velocity.py:55`, in the Breakout engine.

Today it changes nothing at either engine, for two separate reasons, and that
is its own problem: the Bounce engine never calls either function (its
session rule is inline, now `_gates.asian_counter_bias_blocks`), and the
Breakout velocity monitor refuses the Asian session unconditionally on the line
after it asks. So the parameter is tuned, clamped, logged to the learning
history, and connected to nothing. `docs/todo/bugs/045`.

The comment above those imports says *"pure functions with no side effects,
importing is safe and DRY"*. It is true of `compute_adx` and the rest of that
list. It is not true of `session_quality`, which reads another engine's store.

**If you wire `session_is_active` into the Bounce engine** — an obvious
tidy-up — 38% of that engine's signals disappear immediately, because the
stored value is `0.0`. Read bugs/045 first.
