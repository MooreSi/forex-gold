# Engines

**Living file — update when this domain teaches you something.**
Covers: `backend/src/services/breakout_signal/`, `reversal_engine/`,
`trend_pa/`, `backtest/`.

## What it is

Two independent research engines each generate their own XAUUSD signals,
track them virtually, learn from outcomes, and — only when their own
live-execution toggle is on — place real MT5 orders through the main engine:

- **Breakout** — trend-following break-and-go / break-and-retest
- **Reversal Engine** — Gold Diggers VIP / GD2 ICT emulation

There were three. **Bounce / TestSignal** (mean-reversion off key levels) lost
its panel on 2026-09-02, was stopped on 2026-09-13 and was deleted on
2026-09-14 — `docs/todo/bugs/046` is the whole arc. Its name still occupies
position 1 of 3 in `engines_controller._ENGINE_SERVICES`, bound to `None`,
because the sync protocol binds engines by that fixed order.

It stayed on Trading > Strategy (and the Telegram panel's channel picker, and
the channel-strategy sync snapshot) until 2026-09-29, because
`channels/repo._FIXED_ENGINE_CHANNELS` still seeded "Bounce Engine" into every
channel list. It now lives in `_RETIRED_ENGINE_CHANNELS`: listed nowhere a
channel can be picked, still counted by `internal_engine_names()` so a
historical Bounce trade is never mistaken for a Telegram channel's. Pinned by
`tests/core/test_bounce_is_off_the_strategy_page.py`.

Each owns an isolated SQLite database, its own adaptive parameters, and its
own ML model, with no cross-training. A separate backtest package replays
recorded candles against the live strategy management rules.

## Where the code lives

- `services/market/` — the primitives both engines share, owned by neither: `sessions.py` (`get_session`, `session_quality`, `session_is_active`), `levels.py` (`compute_htf_bias`, `identify_key_levels`, `is_news_window`), `indicators.py` (`compute_h4_bias`, `compute_adx`, `compute_macd_hist`, `detect_regime`), `macro_context.py` (yfinance), `news_window.py` (Forex Factory). All five were inside `test_signal/` until 2026-09-14, which is why deleting that package had to be a move first and a delete second.
- `services/breakout_signal/` — same shape: orchestrator, manage/live_execute/velocity/learn, generator, `bo_config` params, 22-feature ML, `bo_`-prefixed store, and `backtest.py` (walk-forward harness)
- `services/reversal_engine/` — orchestrator (levels → pending zone signals → trigger → outcomes → REF correlation), TP1–TP8 ladder management, live execute, `level_detector.py` / `ict_patterns.py` (FVG-iFVG-sweep-breaker "Unicorn"), nightly 22:00 Europe/London Telegram+image research sweep, dual-axis ML, `re_`-prefixed store
- `services/backtest/engine.py` / `simulators.py` / `repo.py` — XAUUSD backtest engine, per-strategy `_simulate_*` walkers, main-DB signal reads

## Constraints / must not change

- **The two engines do not share a word for "this one actually traded" (2026-09-16, bugs/062).** `re_signals.live_exec_status` carries `'executed'`; `bo_signals` carries `'success'`, plus `skipped:*` and `failed:*`. The breakout `measure_repo` was modelled on the reversal engine's and kept `'executed'`, so all three excursion queries matched zero rows — on 124 stored signals and on every future one. Nothing caught it: the tests shared the literal with the query, and the mutants only ever varied code that was internally consistent. **Isolation between the engines is not only the databases — it is the vocabulary.** Copying a query from one engine to the other means re-checking every literal in it against what that engine writes. The value now has a name, `breakout_signal_repo.LIVE_EXEC_SUCCESS`; three sites on the order path still spell it out and its comment lists them.

- **`bo_signals.atr_m15` holds an M5 ATR (2026-09-16, bugs/063).** The breakout engine fetches M5 only and computes `compute_atr(m5_candles[-20:], 14)`. Every consumer uses it consistently, so nothing is miscalculated — but the AI reviewer's prompt states it as `ATR(M15)`, which is a claim the model cannot check, and the column name misleads anyone reading the table. Measured over the same 300-minute span on 2026-09-16: ATR(M5)=16.27 against ATR(M15)=19.93. The column name stays; the prompt label is the owner's call because that gate can veto a signal.

- **Total isolation between engines**: separate SQLite DBs (`breakout_signal.db`, `reversal_engine.db`), no shared tables or connections, no cross-contamination of ML labels or params. `test_signal.db` still exists on disk and still holds the Bounce engine's 173 signals; nothing reads it but `analytics/signal_lab_repo.py`.
- Each engine has exactly one real-money surface file (`*_live_execute.py`), gated on its own live-execution toggle. Everything else is virtual tracking with read-only bridge access.
- Adaptive params: every Claude-recommended value is clamped to its `[min, max]` envelope before being applied — "the engine never operates outside the safe envelope."
- Backtest design principles: signals tested only forward from creation time; pre-filtered to the loaded candle window; corrupt signals rejected up front; lot size recomputed on current equity after every trade; commission always deducted.
- `backtest/engine.py`'s Reversal Runner constants must stay in sync with the live `_GDVR_*` values; `simulators.py` is imported lazily to avoid an import cycle.
- `reversal_engine` implements only publicly documented ICT definitions from plain OHLC — no proprietary indicator code.

## Known things & gotchas

- **Where the engines run (2026-09-29).** Breakout, Reversal and Trend PA ask `node_roles.engines_generate_here()` at the top of each cycle: the node that trades, or the Mac when `centralized_signal_gen_enabled` is on (it forwards). Before this they refused to run on the VPS at all, so with centralized mode off nothing generated anywhere. The engines' research and training timers stay Mac-only; the VPS gets models by the manual snapshot upload (Settings > Remote node), which carries `trend_pa_model.pkl` too since 2026-09-29 (`model_transfer._MODEL_GLOBS`). See the platform README, "Telegram and the engines run on the node that trades".

- **The Reversal Engine's LIMIT ORDER path runs a single-mode EA Template's rules (2026-09-28, live ticket 2103677613).** `_try_re_limit_order` sent the RE signal's own ladder, stop and size with `strategy="template:<name>"` and no template on the wire, so the EA ran none of the template's partials, breakeven or trail. Live: a BUY filled at 4142.02 carrying 8 RE levels (four of them below the fill), a 103-pip stop against the template's 50, and a broker TP that was just the ladder's highest level. The grid-template half of this was fixed earlier by handing grid templates back to the staging path; single mode was left on the old shape. It now does what `limit_order_signal` has done since limit-orders/020: `resolve_template_tps(template, direction, PriceRef(price), ...)` and `template_levels.template_sl_at(template, direction, price)`, both measured from the RESTING price (a BuyLimit/SellLimit fills there or better, so every TP is beyond the fill), `lot_sizing.template_lot` from that stop, `refuse_unplaceable`, and `template=` passed to `place_pending_order`. A strategy that is not a template is sent exactly as before. **Not yet run against a demo account.** Pinned by `tests/trading/test_re_limit_order_sends_the_template.py`.

- **Three Reversal labels, three different trades (2026-09-30, reversal-engine/250).** `_realised_r` (the v9 label) is the engine's own virtual ladder at 0.1 lot, EXCEPT on executed rows, where `net_pnl_dollars` is the broker's profit at the real lot under the template's stop: measured on 232 of them, the label reads a median 0.73x the real R. `tpl_r` replays every signal through ONE template ("30 TP1 SL50 and Trail", approximated as one partial and a runner). `own_template_label` replays each signal through the template in its `strategy` with `backtest/template_simulator` and refuses harvest, grid, resting-leg, staged-trail and dynamic-ATR templates; it agrees with the broker's sign 97% (rank corr 0.91) against `tpl_r`'s 91%, and runs about 0.065R pessimistic. It is research-only (`tools.re_measurement_audit`); nothing the app fits reads it. Under all of them the engine loses about 0.14R a trade and no walk-forward model selects winners. Templates keep no edit history, so any replay uses today's definition.
- **The Breakout ML hands the previous model over on a version bump too (2026-10-01).** `MODEL_VERSION` is `N_FEATURES` and `_model_path` embeds it, so the first start after a feature is appended finds no file at the new version. It used to start with no batch model and no online learner, and `has_batch()` False means the live gate (`ml_prob is not None and bo_ml.has_batch()`) blocks nothing. `init()` retrains from the database straight away, which closes the window only when that retrain succeeds on at least `MIN_TRAIN_SAMPLES` closed rows, and the online SGD learner was always lost. `breakout_signal/ml_handover.py` now finds the highest older `bo_ml_*_v<N>` files, loads them, and `predict()` / `_online_update` score only the leading N features (append-only, as `_pad_legacy` assumes). Three silent failures it closes: a full-width row into a narrower model raises inside `predict()`'s broad `except` and returns None (gate open); `_save_all` would write the narrow model under the new name and the next start would load it as current; the old SGD pipeline would reject every full-width update at debug level. While handed over `_save_all` writes nothing (the old files stay, so a restart hands over again), `init()` retrains anyway, and a successful retrain ends the handover and drops the stale online model. The floor is v15, the shortest vector `_pad_legacy` replays; there is no label epoch because the label has been rr_tp1 / -1 / 0 throughout (the git history here starts at the 2026-09 relocation, so that is read from the code, not from history). **Not yet exercised on a real v22 -> v23 bump.** Pinned by `tests/breakout_signal/test_ml_version_handover.py`.
- **Event-distance study (2026-10-01).** `tools/event_buckets.py` (logic in `backtest/event_buckets.py`) buckets closed signals by signed minutes to NFP / FOMC and the "none" baseline. On the 2026-07-20..10-01 data it finds nothing: 4 events, 129 Reversal signals within two hours of one, every bucket under the thin threshold, no bucket clearly different from baseline. That is absence of evidence, not evidence of absence. The calendar feed keeps one week, so more events need `--events file.csv`, and CPI dates are deliberately not guessed. An event-radar ML feature is not justified by this; a post-release blackout is the cheaper thing to test first.
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

- **A velocity-triggered suppression in Breakout writes no analysis row (found 2026-09-19).** Every `log_analysis` call on a refusal path in `_process_candidate` sits behind `if not velocity:` -- blocked level type, level cooldown, consecutive-loss cooldown, cross-engine conflict, failed risk calc and duplicate direction all return silently when the candidate came from the velocity monitor. Only a *created* velocity signal is logged. So the analysis log answers "why did the M5 cycle not fire?" and cannot answer the same question for the faster path, and any count of suppressions by reason under-reports by however much velocity contributed. This is deliberate in the code, not a bug, but it is invisible from the panel: a velocity candidate that was suppressed and one that was never generated look identical. Characterised by `TestTheVelocityLogAsymmetry` in `tests/breakout_signal/test_process_candidate_suppressors.py` so a change to it is a decision.
- **The bootstrap override is the one place a Claude rejection is overturned.** With fewer than `_BOOTSTRAP_SAMPLES` (20) labelled rows, a rejected Breakout candidate is accepted anyway to build training data, with `[bootstrap]` prefixed to the rationale. It correctly does **not** fire when `review["fallback"]` is true: a fallback means the reviewer *errored*, and overriding that would turn a Claude outage into a stream of unreviewed signals. That distinction was untested until 2026-09-19 and a mutant removing it survived nothing only because a test was written for it -- see `TestTheBootstrapOverride::test_a_claude_ERROR_is_never_overridden`.

- **Breakout's MT5 closure sync cannot distinguish a read failure from an open position (found 2026-09-19).** In `_check_outcomes`, the block that copies a live trade's real result back sits entirely inside a bare `except Exception` logged at DEBUG. If `fetch_main_close_ro` raises -- a database error, a schema change, a ticket that does not match -- the signal simply falls through to the next pass, which is the same thing that happens when the position is genuinely still open. It will retry forever, silently, and the engine will never learn from that trade. Demonstrated by mutation: replacing the `if _mrow:` guard with `if True:` is an EQUIVALENT mutant, because the guard and the except clause produce identical observable behaviour. This matters more than a normal swallowed exception because `_close_and_learn` writes the ML label -- a trade stuck here is a trade the model never sees. Not changed, because changing it changes what gets learned from a live trade; the tests are in `tests/breakout_signal/test_outcome_sync_and_triggering.py`.

- **`_close_and_learn`'s `outcome` parameter is dead (found 2026-09-19).** `ml_outcome = outcome` is followed immediately by an exhaustive `if/elif/else` on `net_dol`, so the value every caller passes is discarded without being read. That matters because callers do real work to produce it: the ladder distinguishes a stop that was moved to breakeven (`"be"`) from a genuine stop-out (`"loss"`), and `_check_outcomes` derives its value from the broker's ACTUAL profit before handing it over. None of it survives the first four lines. Pinned by `TestTheOutcomeArgumentIsDiscarded` in `tests/breakout_signal/test_close_and_learn_arithmetic.py`. Not changed: it decides the ML label on real trades.
- **A live trade can be labelled from the virtual calculation.** After recomputing from the virtual close price, `_close_and_learn` re-reads the real figure with `fetch_main_mt5_profit` and overrides -- but that block is a `try/except` logged at DEBUG. If the lookup fails, the outcome recorded and taught to the model is the simulated one, on a position that really traded, even though the broker's number was already known to the caller and passed in as the argument described above. Same family as the closure-sync gap noted earlier in this file: on this engine the failure mode of a swallowed exception is not a missing number, it is a wrong training label.
- **The time stop's `"be"` outcome is unreachable in practice.** The gate is `unreal < -0.2 * sl_dist`, and the `"be"` label requires `unreal >= -1.0`. For any `sl_dist` of 5.0 or more the gate already sits at or below -1.0, so no value satisfies both and every time stop reports `"loss"`. XAUUSD stops are ATR-derived and comfortably past 5.0. The branch is only reachable with a stop tighter than $5; both cases are pinned in `tests/breakout_signal/test_management_ladder.py`. Moot in its effect today, because of the dead parameter above -- but the two together mean the time stop's intent has never reached the database.

- **Neither Reversal model has an edge out of sample (measured 2026-09-22).** Purged 4-fold, 1h embargo, on the 5,532 closed signals with features, label = net P&L > 0 (virtual net already charges spread/commission, so `cost_r = 0`). The production LightGBM set-up (`_retrain`: 100 trees, `min_child_samples=5`, refit every 5 closes, no holdout) scores AUC **0.521** on all history and **0.490** since v9; the live model's own stored `ml_prob` ranks the signals since 5 Sep at **0.491**. A regularised LightGBM: 0.532 / 0.511. `meta_label.MetaLabeller` reaches **0.553** on both, so it would clear its own 0.55 bar -- but **no band of its score has positive expectancy**: its top quintile averages -0.086R on all history and -0.018R since 5 Sep, and `p >= 0.8` gives -0.095R / -0.016R. It lifts the win rate, not the payoff. So the losses are the generator's, not the filter's, and no model fed these features will turn the engine profitable. Also: **until 2026-09-22 nothing in the app called `MetaLabeller.fit`** -- its "never fitted" status was structural, not a matter of waiting for data. `meta_label_schedule.meta_label_refit_sweep` now fits it on the research loop's minute timer, once per London day and on the first pass after a restart (the model is in memory, so the last-fit date is process state, not app_config). It trains on `meta_label.rows_from_signals` with `cost_r = 0.0` because `net_pnl_dollars` is already net of costs. Fitting changes no decision: the live path consults it only when `meta_label_gate_enabled` is on. **But the nightly AI tuner may switch that on** (`ai_tuner.TUNABLE`), and until now doing so was a no-op; once the model arms it is not. `_retrain` stores only `n` and `mean_r`, so no out-of-sample figure for the production model existed anywhere before this measurement.

- **The engine's win rate is its geometry (measured 2026-09-23, `chance_benchmark.py`).** A no-edge entry between a stop `SL` away and a target `TP` away wins `SL/(SL+TP)`. Virtual signals: chance 75.5%, actual 74.4% over 4,681. **Executed rows cannot be scored this way**: the EA template closes them at its own stop and target, not `sl_dist`/`tp1`, and scoring them against the engine's geometry read 874 trades as z = -11. So the benchmark's verdicts are virtual-only and executed trades are reported apart. The same trap applies to anything that joins `re_signals` geometry to a live outcome.

- **Other markets come from the bridge's RANGE path, never `/candles_symbol` (2026-09-23, reversal-engine/230).** `/candles_symbol` is `copy_rates_from_pos`, and for a symbol not in MT5's Market Watch it returned the terminal's cached history: XAGUSD 11.7h stale, USDX 12 days, USDCHF 113 days, with no error. `/candles_range?symbol=` (`copy_rates_range`) returned the same symbols minutes old. `research_lab`'s `correlation.snapshot` still uses the stale path. `cross_asset.peer_features` also refuses a peer whose last bar is over 15 minutes old at the signal's time, so a stale series becomes "missing", not a false reading.
- **The bridge stamps other symbols' bars seconds off the boundary (2026-09-23).** Gold's M5 bars are exact; silver's are +1s, the S&P's +9s, VIX's +40s. Anything that pairs bars across symbols by timestamp must snap them to the bar boundary first (`cross_asset._closed_before` does). Unsnapped, VIX correlated with gold on 0 of 6,570 signals and the S&P on 645. **`market/correlation.align` pairs on exact timestamps and does not snap**, so `correlation.snapshot` (the research study's `cross_asset` section) is affected by the same offsets.
- **First measurement (2026-09-23, 5,574 closed signals, back-filled offline):** the peers move with gold as expected (mean six-hour correlation: silver +0.83, platinum +0.65, S&P +0.39, dollar index -0.48, USDJPY -0.34, oil -0.25, VIX -0.24). None of that predicts the engine's outcomes. Meta-labeller AUC is 0.553 without the peers and 0.558 with them, on the same rows. Each peer's own 60-minute move scores 0.479-0.509, and the with-peers model's top fifth still averages -0.045R. Moving with gold is not the same as saying whether a gold entry works.
- **Cross-asset features live beside the model, not in it.** `re_signals.xasset_json` holds a per-signal snapshot written by `xasset_sweep`. The production `ml_engine` vector is unchanged (v9). Adding to it is a version bump that changes which demo trades the gate passes even with the toggle off. The meta-labeller appends `cross_asset.vector` only when `re_xasset_features_enabled` is on, and every refit fits both variants on the same signals and records both AUCs in `re_xasset_fits`.
- **`meta_label.auc` is rank-based since 2026-09-23.** It matches the old pairwise version exactly on 500 random cases, ties included, and cuts a refit on 5,500 rows from ~10s to a fraction of a second per model. `chance_benchmark` reuses it.

- **The reference channel's "confirmation" is hindsight (measured 2026-09-24).** Signals the REF channel later matched win 83.0% against a 76.0% chance rate (z = +4.4, +$1.41 a signal, 705 signals). Split by when the channel posted: before our trigger, z = +0.7 and -$3.10; after our trade had already CLOSED, z = +4.9 and +$4.45. The channel posts after price has bounced. `re_require_ref_confirmation` can only use the first group, which has no edge. Do not cite the pooled number as a reason to switch it on.
- **A bar replay must not fill in the bar the signal was created in (2026-09-24, reversal-engine/240).** That bar's open, and its range up to the creation moment, predate the signal. The first run of `tools/re_entry_study.py` did, and the cohort it flattered (signals created while price sat at the level) read z = +4.7 and +0.06R. With the fill moved to the first bar that opens after creation it reads z = -2.3. `entry_study.find_touch` is pinned by `test_the_bar_the_signal_was_created_in_is_not_a_fill`.
- **Overlapping trades inflate a z-score (2026-09-24).** Entries minutes apart ride one move. On a driftless random walk with an entry every 14 bars the textbook `sqrt(Σp(1-p))` gave z = 3.15; the cluster-robust variance over 2-hour blocks does not. The engine fires ~150 overlapping signals a day, so every z in `chance_benchmark` (which uses the textbook form) is somewhat overstated. `entry_study.beats_placebo` and `edge_model.prove` use clusters.
- **Replaying on M1 bars is faithful enough to judge entries (2026-09-24).** Against 211 real executed trades, the template replay agrees on the sign 86% of the time (correlation 0.80). Its mean is more pessimistic than the broker's (-0.231R against -0.132R with the strict fill; -0.152R with the creation-bar fill): the live fill is seconds after the touch, the strict replay a whole bar.
- **No entry rule, approach feature or model beats a random entry (2026-09-24, 5,516 signals, reversal-engine/240).** Against placebo entries on the same tape: the touch entry wins 48.0% at stop 5 / target 5 against 49.9% (z = -3.2), -0.155R after costs. Waiting for a pierce-and-close-back confirmation is worse (-0.28R at 1R). None of approach speed (5/15/60 bars), range expansion, tick-volume surge, touches in the last day, minutes since the last touch, 4-hour range, stretch from the hourly mean or hour of day has a quintile at z >= +2. A walk-forward LightGBM and a logistic model on all of them score AUC 0.49-0.50. The template's own exits, replayed from each real trigger over 5,645 closed signals, average **-0.150R**.
- **`meta_label_gate_enabled` was ON on demo on 2026-09-24** at threshold 0.5, although the 2026-09-23 commit that armed the model said it would stay off. Nothing logs who changes that switch. Its armed model's best fifth loses money.
- **The proven-edge gate refuses when it cannot score (2026-09-24).** Every other model gate on `_try_live_execute` passes a signal when its model has no opinion or the fill-time re-evaluation failed. `re_require_proven_edge` does the opposite on purpose: `_edge_feats` stays None if the re-score raises, and `edge_model.decide(None)` is a refusal. Pinned by `test_when_the_fill_time_rescore_could_not_run_it_refuses`.

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
- **The research study runs nightly, and the tuner now says how old it is**
  (2026-09-16). `research_lab.run_study` is the only producer of the reach
  distribution and the exit-policy sweep, and `ai_tuner.gather_evidence` can
  only READ the stored summary -- it cannot refresh it. While the study was
  button-only it went four days stale (last run 2026-09-12) and the tuner
  kept presenting that sweep as the current reach evidence, which is the
  slow-fuse version of the 2026-09-11 failure already recorded in
  `ai_tuner`'s own docstring: a 2.0x ATR target proposed from half a picture.
  `study_schedule.py` now runs it once a day from the minute timer
  `research_loop` already owns, deduped by the `re_study_last` app_config key
  and gated to the local node, and `gather_evidence` reports
  `study_age_hours` on every pass plus a `study_note` past 36 hours.
  **The schedule does not make the engine self-tuning** -- `_ai_tune_loop` is
  still inert behind `re_ai_tuning_enabled`, which is not even a column in
  `vantage_risk_settings`, so nothing acts on the study automatically. It
  makes the Recommend button honest.
- **22:00 Europe/London is the settlement break, not just "late"** (2026-09-16).
  17:00 New York is 21:00 UTC under EDT and 22:00 UTC under EST, and London
  local tracks that shift both ways, which is why both nightly jobs use
  London time rather than UTC. Over the seven days to 2026-09-16 the engine's
  `re_analysis_log` produced **2 signals in the 21:00 UTC hour** against 17-72
  in every other hour. That is the mitigation for the study's bridge load and
  the limit is worth stating: bugs/030 moved the study's CPU-heavy arithmetic
  off the loop on 2026-09-12 and deliberately left its database and bridge
  reads on it. ~250 sequential `get_ticks_range` calls at ~0.19s each still
  saturate the bridge for minutes; they await properly so they cannot freeze
  dispatch, but scheduling them into the quiet hour is not the same as making
  them cheap.
- **`ai_tuner.auto_tune` logs every pass, including the ones that change
  nothing** (2026-09-14). It used to log only an APPLIED setting, which made
  four different outcomes identical silence: the model weighed the evidence
  and declined, the provider was down, the response could not be parsed, and
  `sanitise` dropped the whole proposal. On a loop that runs every fifteen
  minutes against an engine with `re_live_execution` on, that is the
  difference between a working tuner and a dead one. `_ai_tune_loop` still
  discards `result["error"]` -- the log line is inside `auto_tune`, where the
  rationale actually is, which also keeps the service under its ceiling.
- **Until then, the only way to prove the tuner had run was the httpx log.**
  `gather_evidence` leaves a signature immediately before its provider POST:
  `GET /candles/XAUUSD?timeframe=H1&count=60`, then `GET /ticks?from=…&to=…`
  over a 900-second window, then `GET /tick/XAUUSD`. Matching those against
  engine-start + n*900s is how the 2026-09-14 session confirmed two live
  passes that had written nothing and said nothing. Worth keeping: the same
  trick identifies any loop that calls an AI provider.
- **Neither `reversal_ai_apply` nor the Save Tuning button logs
  anything**, so a switch that changed cannot be attributed to the AI or to
  the owner after the fact. Only the absence of an `[RE-AI]` line rules the
  tuner out. Not fixed -- recorded because it cost a session's worth of
  inference to establish once.

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
`market/sessions.get_session` calls **23:00-07:59** Asian, and
`reversal_engine/level_detector.get_session` calls it **00:00-07:59**. The
23:00 hour is in one engine's rule and outside the other's. The Reversal Engine's own numbers over the same hours
say the opposite — see the risk domain README and
`capability_gates.asian_bias_exempt`. The two engines trade different setups,
so it is possible both are right; nothing on this engine was changed on the
strength of the other's data. It is the owner's call:
`docs/simon-handover/033`.

## The engines were not as isolated as the top of this file said (2026-09-12, resolved 2026-09-14)

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

The comment above those imports said *"pure functions with no side effects,
importing is safe and DRY"*. It was true of `compute_adx` and the rest of that
list. It was not true of `session_quality`, which read another engine's store.

**Resolved 2026-09-14 by deleting the Bounce engine.** `session_quality` is now
in `market/sessions.py` and reads no parameter at all: the stored `0.0` is a
named constant, `ASIAN_SESSION_QUALITY = "low"`, preserving today's behaviour
exactly. The claim at the top of this file is now true of the two engines that
remain — they share `services/market/`, which is pure functions over candles
and holds no engine's state.

The lesson generalises past this instance: **a function one engine imports from
another engine's package is a coupling whatever its docstring says.** The tell
here was not the import, which looked harmless, but that one of the ten names
in it reached for a store. Nine did not. Reviewing the list as a list is how
that survived for months.

## The ICT chain, and a hole in its first stage (2026-09-12)

`reversal_engine/ict_patterns.py` is four decisions in a row —
`detect_equal_levels` → `detect_liquidity_sweep` →
`detect_market_structure_shift` → FVG/breaker confluence — and
`find_unicorn_setup` runs all four. Its output becomes a real order: this is
the only engine with live execution on. It sat at 56.6% coverage with the whole
chain untested; `tests/reversal_engine/test_ict_pattern_chain.py` now covers it
(21 cases, seven mutants killed).

Things worth knowing, found by writing them:

- **`detect_equal_levels` cannot see exactly equal highs.** The candidate list
  goes through `set()`, so two candles whose highs round to the same 0.1
  collapse to one value and the pool never forms. 110.0 + 110.4 is a pool;
  110.0 + 110.0 is nothing. `docs/todo/bugs/047` — not fixed, because dropping
  the `set()` changes which pools exist and rescales every touch count on the
  live path.
- **The sweep test is close-back, not wick-through.** A candle that pokes
  through a pool and closes beyond it is a break, and is deliberately not a
  sweep — trading it as a reversal would be trading into a trend.
- **The two filters fail in opposite directions, on purpose.**
  `detect_market_structure_shift` fails CLOSED on insufficient history (it is a
  confirmation: no evidence means no shift), while the breakout engine's
  `_adx_rising` fails OPEN (it is a veto, and one that blocks when it cannot
  judge stops the engine trading at all).
- **`find_unicorn_setup` gives up rather than quoting a zone it does not
  believe.** A confluence wider than 15 points or narrower than 0.5 falls back
  to the FVG's own bounds, and if that fails the same test it returns None.

## What the champion/challenger shadow log can and cannot answer (2026-09-12)

`reversal_engine/shadow.record_all` sits deep inside `_live_execute`, below the
bias gate, the ML floor, the momentum gate, the exposure guard, the schedule,
the news blackout and the fill-delay check, and above the liquidity gate, the
entry trigger and the meta-label gate. That position is deliberate and the code
says why: recording at each early return would log "would take" for a variant
whose later gates never ran, which reads as an endorsement it never gave.

**The consequence is not written down anywhere, so here it is: a challenger can
only differ on the last three gates.** Any variant whose difference is upstream
— a different bias policy, a different ML floor placement, a different schedule
— never sees the signals it would have decided differently about, because those
signals returned before the recorder. The log cannot say it was wrong; it
cannot say anything at all.

That bears directly on `docs/simon-handover/033`. The Asian-session exemption
is a **bias gate** variant, which is upstream. Shadow-logging it would produce
nothing, whichever way it was set, so a live demo session really is the only
way to attribute it — which is what that file recommends, now for a second
reason.

State as of 2026-09-12: 60 rows, 12 signals, all on 2026-09-11 between 14:31
and 18:45 (the afternoon it shipped). Three of the five arms — `confirmed
entries`, `liquidity aware`, `meta 0.55` — have **not disagreed with the
champion once**. Only `ML floor 0.50` differs, refusing six of the twelve. So
the comparison is not yet discriminating between four of its five arms, and a
reader glancing at it should know the sample is one afternoon rather than a
week.

## The card is "Reversal Engine Tuning", and one switch on it is inert (2026-09-17)

Renamed from "Reversal Engine Capabilities" at the owner's request. The
label lives in `frontend/pages/reversal_panel/_capabilities.py` (file name
unchanged) and is pinned as a render landmark in
`tests/frontend/test_remaining_pages_render.py`. Two docs that named the old
path were corrected in the same change; `docs/todo/reversal-engine/210`
keeps the old name as history with a note, because it is a record of what
was built rather than a description of what is there now.

**`re_cme_context_enabled` (migration 46) is wired to nothing, deliberately.**
`capability_gates.cme_context_enabled` is its only reader and nothing
consumes that reader. The reason it stops there is not effort:

- Spot XAUUSD on this broker publishes bid/ask and no Last, so there is no
  trade side, and every "volume" in this system is tick volume — a count of
  quote changes, not size. `services/market/order_flow.py` already carries
  this, and labels each result with the method that produced it.
- CME GC futures are the lit venue where gold prints real size, and the only
  route from proxy to measurement. Dark pools are an equities construct
  (off-exchange prints reported to a regulator's tape) and do not exist for
  spot gold at all — worth knowing, because it is asked.
- **Cost is not the blocker.** Daily GC volume and open interest are
  published free by CME; only real-time streaming is a paid entitlement and
  this engine has no use for it. The blocker is that nothing here has
  measured whether futures flow predicts anything about these trades, so
  the ingest would be built on a guess. Recorded in
  `docs/simon-handover/039-cme-futures-context-is-free-is-it-worth-building.md`.

Same shape as `vol_target_sizing_enabled`, which has been on this card
un-connected since 2026-09-11. The risk with an inert switch is that
somebody later believes it did something, so the tooltip says "no CME feed"
and "changes nothing" in the app, and
`tests/risk/test_cme_context_switch.py::TestTheCardDoesNotOverclaim` fails
if either phrase is removed.

**It is not in `ai_tuner.TUNABLE`**, and should not be. The tuner argues
from this account's own measured trade history; there is no CME evidence for
it to argue from, so the switch would be a coin flip with a rationale
attached.

## The Reversal Engine retrains off the event loop (2026-09-24)

`record_outcome` ran the batch retrain inline every fifth closed signal, and
the 22:00 nightly research did the same: ~450 ms each (103 ms reading training
data, 354 ms fitting, profiled on a copy of the live data), with the position
monitor, EA link and Telegram all waiting. Now `_request_retrain` schedules
`retrain_async`, which reads and fits on worker threads and installs the model
back on the loop. One at a time; a request during a retrain runs once more
after it. With no running loop (scripts, tests) it retrains inline as before.
`retrain_now` is gone; the nightly job awaits `retrain_async`.

- **The old `_retrain` installed the model before fitting it.** For the length
  of the fit, `_model_batch` was an unfitted LightGBM whose `predict` raises,
  which `predict()` swallows. That was invisible only while nothing could run
  in between, and **the gate fails open.** `_fit_batch` now fits into a local
  and `_install_batch` swaps it in with one assignment, on both paths. Pinned by
  `test_the_old_model_stays_in_place_until_the_new_one_is_fitted`.
- The online SGD update and the pattern stats stay on the loop. They are
  milliseconds, and they mutate state `predict` and `record_level_touch` read
  there.

## Breakout tuning experiment ledger (2026-09-27, docs/todo/007)

`breakout_signal/tuning_ledger.py` (+ `tuning_ledger_repo.py`, table
`bo_tuning_experiments` in `breakout_signal.db`). The batch review
(`_run_batch_analysis`, every 10 closed signals) no longer calls
`apply_adjustment` itself: it hands the model's adjustments to
`tuning_ledger.handle_batch`, which is now the only route from the AI to a
param. Pinned by `tests/breakout_signal/test_batch_review_uses_the_ledger.py`.

- **Record mode (default, `bo_config.tuning_approval_required` = "0")** is
  the old behaviour: every adjustment applied as it arrives, same function,
  same clamps and locks, same "param→value" log string. Each is recorded with
  the baseline (mean net $ of the last `MIN_SAMPLE` closed signals),
  `concurrent` = how many params that batch changed, and after `MIN_SAMPLE`
  closes a verdict (better / worse / same). **It never rolls back.**
- **Approve mode ("1")**: adjustments become `proposed` and change nothing;
  a new batch supersedes older proposals. One experiment runs at a time.
  After `MIN_SAMPLE` closes: worse than baseline -> rolled back, else kept.
  Net since applied <= -`FAILURE_USD` rolls back early. If the param no
  longer holds the experiment's value, a human changed it: `abandoned`,
  never rolled back over the edit.
- Approval and rollback both go through `apply_adjustment`, so a
  `tuner_locked` param can never be proposed, approved or rolled back.
- `evaluate()` runs after every close in `_close_and_learn`, wrapped: a
  ledger failure logs a warning and never breaks a close.
- Provisional defaults, owner decisions: `MIN_SAMPLE` 30, `FAILURE_USD` 100
  (virtual $1,000 account). "Not worse" keeps; there is no significance test,
  and 30 signals is a noisy sample. Stated on the card.
- UI: Signal Generator > Breakout, "Tuning experiments" card;
  `/api/engines/breakout/tuning`.

## GEX snapshots are collected, not used (2026-09-27, docs/todo/009)

`market/gex_collector.py` stores the GLD option chain once a weekday from
22:00 London (research loop timer) into `gex_snapshots` / `gex_strikes` in
reversal_engine.db, with GEX, flip, walls and their XAUUSD equivalents (gold
spot from the Reversal engine's bridge). **Nothing that trades reads it**
(the Dashboard's GEX card shows the latest snapshot through
`market/gex_report.py` and GET `/api/gex/latest`, display only; a snapshot
over 4 days old is flagged stale, weekday collection allowing a weekend
gap). It exists
to build the history yfinance cannot give. The sign convention (dealers long
calls, short puts) is recorded on every row, and the raw per-strike chain is
kept so it can be recomputed. Include every expiry within 60 days: capping at
the first 8 (all dailies) flipped the sign of total GEX on the first live
fetch. No node-role check of its own (the loop's one `is_remote_node` call is
pinned); it runs only where the engine has a bridge.


**The card follows the node that trades (2026-10-02).** The snapshot is read
through `cluster/peer_reports.py`: with the VPS trading, the Mac asks the VPS
for its latest snapshot (`MSG_PEER_REPORT`) instead of its own. The collector
still writes where the Reversal engine has a bridge, so under centralized
signal generation (engines on the Mac, VPS trading) the VPS may hold no
snapshot and the card says "No GEX snapshot yet". That is the trading node's
true state, not a bug to paper over with the Mac's copy. Details in the broker
domain's Fill cost section.

## Trend PA (2026-09-29, docs/todo/012)

The owner's "simpler" engine: H4 swing structure plus an EMA50 filter for the
trend, London and New York only (08:00-21:00 UTC, nothing after 19:00 Friday),
a pullback to an H1 swing level (a broken swing high counts as a floor), a
closed M15 engulfing or pin bar, stop beyond the pullback, target 2R.
`services/trend_pa/`: `strategy.py` (pure, closed candles only), `outcome.py`,
`stats.py`, `backtest.py`, `ml.py`, `repo.py` (`trend_pa.db`), `service.py`,
`live_execute.py` (the one real-money surface), `panel_data.py`.

- **It is reached by NAME, not position.** `registry.ENGINE_NAMES` and
  `all_instances()` are still exactly (breakout, bounce, reversal), pinned by
  `test_handover.test_the_binding_order_is_fixed`; Trend PA sits in
  `_NAMED_SERVICES` and `ALL_NAMES`. The sync server finds it through
  `registry.instance("trend_pa")` in `_sub_engines`, so it is in the
  heartbeat, Start/Stop, stand-down and resume, with no change a paired node
  on an older build can see. `running()` lists it only once built.
- **The stats mirror is one dict**: `signal_gen_stats["trend_pa"]` is
  `panel_data.local_report()`, and in Remote mode `panel_data.report()` returns
  the VPS's copy. The backtest button in Remote mode is `engine_control`
  action `"backtest"`, which the VPS acks BEFORE running (a three-year replay
  outlasts the Mac's 10s ack wait).
- **Generation follows the other engines' node rule**: never on the VPS
  (`is_remote_node`), never on a node centralized generation moved away from.
  Live orders reach the VPS the same way Breakout's do, through
  `open_trade`'s forwarding.
- **The bridge stamps bars in broker time.** At 09:32 UTC the newest M15 bar
  read 12:30. `backtest.BROKER_OFFSET_S` is the 10,800 the rest of the code
  uses; the live cycle uses the wall clock for the session and drops the
  forming bar from every timeframe.
- **Equal swing highs used to count twice.** `strategy.swings` gives a tie to
  the first bar (strictly beyond the left, level-or-beyond the right).
  Without that, a clean staircase whose peak bar shared a high with the next
  bar compared its last two swings as equal and read "no trend".
- **First measurement -- MADE ON THE WRONG CLOCK, see 2026-10-01 below
  (2026-09-29, 533 replayed trades, Nov 2023 - Sep 2026,
  $0.30 a trade):** win rate 35.3% against a 33.3% break-even, +0.054R a
  trade, profit factor 1.08, worst drawdown 28R, t about 0.9 -- not
  distinguishable from zero. Split at 2025-06-01: -0.025R before, +0.126R
  after (the gold trend). A 24-point grid of rr / EMA / chase / tolerance on
  the first half found nothing significant (best t = +0.56 in sample); every
  variant did somewhat better out of sample, which is the regime, not the
  settings. The defaults stay the owner's 1:2.
- **The model has no edge on that data**: logistic regression on the 14
  setup features scored AUC 0.49-0.53 out of sample, so `ml.Model` stays
  UNARMED (needs n >= 60 AND time-ordered holdout AUC >= 0.55) and vetoes
  nothing. Same finding as the Reversal engine's.
- **The live path refuses until a strategy is chosen** for "Trend PA Engine"
  (Trading > Strategy, or a schedule window). Several strategies replace the
  signal's own stop and target, and the 1:2 is the whole strategy, so the
  global default is not assumed. **Not yet run against a demo account.**
- **Silence is checkable (2026-09-29).** No signal means either a quiet market
  or a dead engine, and the panel now says which: `local_report` carries
  `generating_here`, `last_cycle_at` and `last_evaluated_at`, and the panel's
  first line reads "Analysing on the VPS. Last checked 2 min ago." or a stall
  warning after 3 minutes. `evaluate()` also puts what it saw after the
  grouping phrase, "no clear H4 trend (highs rising 4570.1 to 4581.3, lows
  falling ...)"; `backtest.reason_key` cuts at " (" so refusals still count
  together. The replay's own rhythm is the yardstick: a trade every 0.84 days
  at the median, but 75 of 532 gaps were 5 days or longer, longest 15.7.
  The Mac's `trend_pa.db` is NOT the record while the VPS trades: the panel
  reads the VPS's copy (`where: remote`), and the Mac's own engine idles
  ("Generation runs on the local node only").
- **It is a schedule source of its own**, `trend_pa_engine` in
  `risk/schedule.ENGINE_SOURCE_KEYS`, with `trend_pa_engine_override` beside
  it (owner, 2026-09-29). A window saved before it existed reads it as
  allowed with no override: the pre-2026-08-03 shared `strategy_override`
  is deliberately NOT migrated into it, as it was for Reversal and Breakout,
  because it predates this engine. The key is also in
  `template_rename._WINDOW_OVERRIDE_KEYS`, `core_auto_template`'s Auto-source
  sweep and `channel_loss_cap._NOT_A_CHANNEL`; a fourth engine needs all
  four.

### Trend PA, corrected and re-measured (2026-10-01)

Owner: "has also not found a single setup ... could be a defect or the ml
engine". Not the model: it is unarmed (AUC under 0.55) and vetoes nothing.
Live on 29-30 Sep every cycle said "no clear H4 trend", and that was true --
the replay over the same days agrees (gold fell ~$170 on 28 Sep without
printing a lower high, so the last two H4 swings read as an expanding range).
Adding a break-of-structure trend rule was tried and made results worse
(-0.031R over 863 trades), so it was not kept. What was wrong:

- **The replay read true-UTC bars as broker time.** `run_backtest` fetches
  through `/candles_range`, which answers in UTC (`mt5_bridge._get_candles_range`
  converts), and `bt.run` then subtracted `BROKER_OFFSET_S`. The "08:00-21:00"
  replay traded 11:00-24:00 UTC. `bt.run(offset_s=0)` from the engine now; the
  default stays for callers with broker-stamped bars.
- **Measured right, the strategy as specified has no edge:** 640 trades,
  -0.004R, max drawdown 48R. By session: entries 12:00-20:00 UTC made +0.125R
  (401 trades, 41% at 2R, t 1.7, 2024 +0.09 / 2025 +0.14 / 2026 +0.16, max
  drawdown 17.8R, ~2.8 a week); 12-17, 12-18, 13-17 all agree. The London
  morning is what took it to zero -- the same split the ORB replay found.
- **The session is two Expert Tunables**, `tpa_session_start_utc` and
  `tpa_session_end_utc`, defaults 8 and 21 (what shipped). `service.session_params`
  feeds both the live cycle and the replay. A stored replay is tagged
  `backtest_version` (bar clock + session) and redone on the next start when it
  no longer matches, so the panel stops showing numbers from the wrong clock.
- **The live de-dupe assumed +3h.** `close_at` subtracted `BROKER_OFFSET_S`;
  from the broker's DST change (+2h, late October) each signal would have
  blocked the next hour of bars. `service.broker_offset` reads the offset off
  the forming M15 bar, which opened under one bar ago.

Pinned by `tests/trend_pa/test_sessions_and_clock.py`.


## Reversal ML: zeros, handover restarts, fill-time FVG (2026-10-01)

- **A real 0.0 used to reach the model as its default.** `extract_features` read most inputs as `float(x or default)`: a ranging market (`regime_score` 0.0, ADX < 14) became 0.5, an imminent event (`news_proximity_norm` 0.0) became 1.0 "safe", an entry at the gap (`fvg_dist_norm` 0.0) became 5.0 "no gap", a REF post this minute became 240 minutes ago, and a signal on its own level read 5 points away. On the owner's Mac 1,525 stored signals had ADX < 14 and none of 7,232 stored vectors carried regime 0.0. Inputs now go through `_num` (None-check only). The stored history is repaired at read time by `_training_data.stored_vector` where the row can prove it: regime from the `adx` column, distance from `price_at_signal == level_price`. News, FVG distance and minutes-since-REF cannot be recovered from the row and stay as stored. **No `_version` bump:** the label and width are unchanged; until the next retrain (every 5 closes) the live tree places a 0.0 regime on the same side of its lowest split as 0.25. Pinned by `tests/reversal_engine/test_ml_zero_is_a_reading.py`.
- **The online learner reads rows the same way the batch set does.** `record_outcome` used to skip any stored vector not exactly the current width; it now uses `stored_vector` (padded and repaired).
- **A restart in the middle of a handover failed the gate open.** Any save during a handover stamped the meta with the new version while the models were still old-width, and `legacy_width` was memory-only, so the next start fed 38 features to 33-wide models, both raised, `predict()` returned None. `_load_all` now reads the width off the loaded models (`ml_handover.model_width`, batch else online) and resumes the handover. A retrain also drops an online model of the wrong width, and `record_outcome` replaces one, where before it raised silently forever. **A fresh online SGD gets its 40% blend weight from its first sample**, as on a fresh install; unchanged here. Pinned by `tests/reversal_engine/test_ml_handover_survives_a_restart.py`.
- **The fill-time re-score now measures FVG context again** against the fresh candles. It is not an `re_signals` column, so `dict(sig)` had none of it and all four FVG features, plus `pro_fvg_delta` and `pro_likeness`, fell back to "no gap" in the vector the ML gate decides on. `ml_prob_at_fill` stores None when there was no prediction, not 0.0. Pinned by `tests/reversal_engine/test_ml_fill_time_rescore.py`.
- **The "Is it learning?" panel leaves executed rows out**: `fetch_ml_outcome_rows` now selects `live_exec_status`, which `_realised_r` needs to refuse broker-scale P&L. **Training rows are sorted by id** in `_get_training_data`, because `_fit_batch`'s time-decay weights are positional and the query has no ORDER BY. Pinned by `tests/reversal_engine/test_ml_training_rows.py`.
- **Open (owner): `ref_level_win_rate` is wins / matches, not wins / closed matches.** `trades` counts every REF match at match time, `wins` only matched signals that closed as wins, so open or never-closed matches read as losses and a closed loss is counted nowhere. It is also our win rate on REF-matched levels, not the reference channel's. Not changed: `test_ml_realised_r_label.py::test_wins_are_credited_so_the_rate_is_no_longer_stuck_at_zero` and `::test_losses_do_not_inflate_the_win_count` pin the current `wins / trades` definition.

## The virtual ledger answers one question per row (2026-10-02)

The owner saw the same dollar figure down a column ("-$558.40") and read it as
invented. It was one trade repeated once per variant. Three separate faults,
each pinned in `tests/reversal_engine/test_shadow_ledger.py`:

- **A variant that cannot differ from the champion measures nothing.** Four of
  five challengers also carried the champion's ML floor, so on every signal the
  ML gate skipped they were identical to it. `Variant.min_ml_prob` is now
  `None` (no ML opinion) unless the variant is measuring the floor itself. The
  default set is `live (champion)`, `no filter`, `ML floor 0.50`,
  `trigger confirmed`, `liquidity aware`, `meta 0.55`. `confirmed entries` was
  renamed `trigger confirmed`; rows under the old name stay in the table but are
  not shown (`history`/`ledger` filter to live variants).
- **One row per signal.** `shadow.ledger(limit)` states a signal's result once
  and puts each variant's call (`take`, `reason`) beside it; `limit` counts
  signals. `/engines/reversal/report` returns it as `ledger`, and the flat
  `history` stays for callers that want decisions. A refusal now reports every
  gate that refused, not only the first.
- **R from the whole trade.** `r_full = net / (sl_dist * 10)`, because the
  ledger sizes every signal at 0.1 lot. The legacy `r` (last leg's points over
  the stop) called a $21 ladder win on a 7pt stop "0.02R"; it is kept beside
  the new figure, unchanged. A broker-executed trade has `r_full` None (its
  dollars are at the real lot), and an unsettled signal is None, never 0.
  `r_replay` is the `tpl_r` replay figure, shown so the two methods can be
  compared. `report()` gains `avoided_net`, `delta_vs_champion`,
  `mean_r_full`, `mean_net`.

## The pro model's AUC was earning its number from the date (2026-10-02)

Measured on the live corpus: 0.82 with the random stratified folds the model
used, 0.68 with whole days held out, 0.57 trained on the first 60% of time and
scored on the last 40%. Background snapshots are taken every 15 minutes, so
random folds put a snapshot's neighbours on both sides of the split and the
forest partly learned the day (RSI, ATR and price level drift). Its output
ranks our own trades' outcomes at AUC 0.496: it separates the moments a channel
posted from background snapshots, not whether a trade wins.

`pro_model.fit` now scores the gate on **time-blocked folds**
(`_blocked_folds`) and reports a **forward-in-time** `auc_forward` beside it,
never gated on. `_MIN_AUC` is unchanged, so the gate now sits on a lower,
truer number: a model that was in use at 0.82 may stop clearing 0.55. That is
the intended effect, but it changes what the live ML feature does on the next
refit, so watch `pro_likeness` after deploy. A signal captured at several
stages is now one example (latest usable stage), not three. Pinned by
`tests/reversal_engine/test_pro_model_honest_auc.py`.
