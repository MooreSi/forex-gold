# Analytics

**Living file — update when this domain teaches you something.**
Covers: `backend/src/services/analytics/`, `ai/`, `notifications/`.

## What it is

The read side of the app: trade-history and performance queries, per-engine
edge statistics, the ORB/IVB pre-London breakout report, LLM-generated
market and trade commentary, and the email digests that ship those numbers
out. `analytics/` computes; `ai/` asks an LLM (Claude or DeepSeek) for
commentary and recovers signals the deterministic parsers missed;
`notifications/` builds and sends the daily/weekly/ORB emails and owns the
email + Telegram notification config.

## Where the code lives

- `services/analytics/trade_history_repo.py` — SELECT-only queries backing the history views; returns raw rows, not dicts, on purpose
- `services/analytics/read_repo.py` / `pnl.py` — hourly P&L grid, realised 24h P&L, equity drawdown, regime score, signal execution lags
- `services/analytics/ticket_maps.py` — per-ticket lookups merging the cross-node ledger with local rows
- `services/analytics/labels.py`, `formatting.py` — display names, broker-timestamp/duration formatting
- `services/analytics/edge_stats.py` — per-engine edge stats read live from each engine's own DB via `mode=ro` URIs
- `services/analytics/orb_report.py` — the London ORB/IVB report: Asian range as the confirmation filter, the first 15 minutes of London as the traded range (the volume profile went in the 2026-08-01 rebuild)
- `services/analytics/orb_ny.py` — the New York ORB (2026-10-01): the decision `orb_auto_execute` acts on when Expert Tunable `orb_ny_mode` is 1
- `services/analytics/reporting.py`, `ai_analysis.py`, `signal_lab_repo.py` — performance computation, the AI Trade Analysis page's cross-DB gathers, read-only access to `test_signal.db`
- `services/ai/provider.py` — unified `complete()` over Anthropic/DeepSeek, selected by `cfg["ai_provider"]`
- `services/ai/claude_ai.py` — per-use-case prompts, JSON schemas and fallback dicts
- `services/ai/signal_extractor.py` — LLM fallback signal extraction after regex parsers fail
- `services/ai/model_refresh_loop.py`, `commentary_repo.py`, `recovered_repo.py` — daily model-catalogue refresh, persistence
- `services/notifications/email_service.py` — transport only (SMTP / Resend / Mailjet)
- `services/notifications/email_html.py`, `scheduler.py`, `config.py`, `repo.py` — HTML presentation, the per-cycle scheduler sweep, config

## Constraints / must not change

- `analytics/` is **SELECT-only by contract**. Anything that writes, or can reach the broker, belongs in another service. (`orb_auto_execute` lives in trading for exactly this reason.)
- `ai/` touches no broker: the AI-fallback path that makes a real `modify_order` call lives in trading, not here.
- `edge_stats.py` and `signal_lab_repo.py` open SQLite with `mode=ro` URIs — read-only by construction, not convention.
- `ticket_maps.py` merge precedence is behaviour: ledger first, local second, so local rows overwrite ledger rows. Reversing it degrades every locally-opened trade to the peer's summary.
- The `open_time >= cutoff` predicate in `trade_history_repo.py` must stay — dropping it turns a bounded scan into a full-table scan on a forever-growing DB.
- `formatting.format_broker_ts` deliberately interprets MT5's UTC+3-encoded epoch naively (yields broker time for display); `broker_ts_to_uk_date` subtracts the offset first.

## Known things & gotchas
- **The Trades page's R:R column is realised R, and the ledger's `rr` is not (2026-09-30).** For a trade this node opened, `ticket_maps.rr_map` gives net P&L over `initial_risk` (`max_tp_repo.get_rr_map_by_ticket`). For a trade the OTHER node opened it used to give the ledger's `rr`, which the frozen `close_trade` computes as |tp1-entry| / |entry-stop_loss| with the stop AT CLOSE: never negative, and 57.18 on a demo loss once a trail had parked the stop beside entry. Owner report: "all over the place and not accurate". The ledger now carries `r_realised`, which the owning node fills from its own figure on each Max TP pass (`positions/max_tp.fill_realised_r`) and the Mac's ledger pull carries across; `rr` is still written, and `get_consolidated_extra_maps` returns `COALESCE(r_realised, rr)`, so until the node that opened a trade runs this code (or for a trade it holds no risk figure for) the other node still shows the old close-time ratio. Blanking those instead was held back: `tests/services/test_ticket_maps.py` and `tests/services/cluster/test_sync_repo_reads.py` pin the ledger `rr` reaching the page, and changing them is the owner's call. A realised R below -1 is usually real: the -12.7R rows of 2026-09-04 are trades that closed while the terminal was disconnected.
- **A backtest row of zeros is not a result — it may be a refusal, and it has to say so (2026-09-04).** `30 TP1 SL50 and Trail` came back `0 trades / 0% / +$0.00 / PF ∞ / final $1,000.00` beside two templates showing 90 trades each. Nothing was wrong with it: the run was tick-based and the template uses `trail_mode=candle`, which the **tick** walk refuses on purpose (`CandleTrailLevel()` trails to the last 3 closed M15 candles; the tick walk has bid/ask and no candle series). The bar walk supports it, so the same template simulates on bars and is refused on ticks. `_simulate_template` catches `UnsupportedTemplate` and returns `None` per signal, and 90 `None`s aggregate into zeros. Returning `None` rather than approximating is correct and must not change — "a plausible number from a template the walk cannot model is worse than no number, because it would be used to choose what trades real money" — but zeros in a comparison table read as *traded nothing, lost nothing*, which beside a row showing 62.8% max drawdown is an argument FOR the template that could not be simulated at all. `StrategyStats.unsupported_reason` now carries the refusal and the table renders "NOT SIMULATED" with the reason. Deliberately narrow: a **built-in** strategy on ticks is not "unsupported" (it is simply not walked), and a **missing** template is a different problem again — labelling it unsupported sends the user to edit a trail mode on a template that is not there. Pinned by `tests/backtest/test_unsupported_template_reason.py`.
- **A strategy's configured TP ladder is not the ladder its trades climb, and the AI prompt must be told both (2026-09-04, live).** The AI Analysis tab recommended `GD VIP - Single` on the reasoning that its eight rungs (20/40/60/80/100/120/170/270 pips) would "capture a continued move" and "let profits run". Its own 30 days said otherwise: of 85 closed trades, 50 topped out at TP1-TP2, 3 reached TP5+, the highest rung ever reached was TP6 (so TP7 and TP8 have **never** been hit), and 62 banked a rung and were then stopped out. The cause is geometry, not luck — the trail arms at 40 pips (TP2) with a 50-pip distance, and H1 ATR that morning was 118-158 pips, so a 50-pip give-back is a fraction of a normal hour and truncates the ladder every time. `request_market_analysis` could not have known: its prompt carried bid/ask, spread, 20 M5 closes, session high/low and a direction word — **no ATR, no volatility measure at all**, and no history of where trades finish. It was handed specifications and asked to reason about behaviour, and produced a fluent, confident, false story. Fixed by feeding it H1/M15 ATR **in pips** (the same unit as every trail and rung, so no conversion stands between the model and the comparison) plus `get_strategy_ladder_reach`, and by an unconditional rule that a trail arming at or below an early rung makes the upper rungs decoration. Note the recommendation itself was not wrong — that template is the best performer on the book — but the reason was, and a wrong reason generalises wrongly. Pinned by `tests/core/test_market_analysis_volatility_context.py` and `tests/core/test_strategy_ladder_reach.py`.
- **A backtest half continued from the other half's closing balance is not a second measurement (2026-09-15).** `run_backtest` recomputes lot size on current equity after every trade, which is right for a single run and wrong for a comparison: a profitable in-sample half leaves a larger balance, the out-of-sample half is then sized off it, and its dollar P&L is scaled by how the first half went. The side that is supposed to be uncontaminated is contaminated by exactly the thing being tested. `split.py` therefore runs each side as its own account from the same `starting_balance`, which is also what `run_backtest`'s own docstring already promises ("results are not cross-contaminated"). `equity_curve[0]` is not enough to pin this — an implementation can reset the curve for display and still have sized every lot off the inflated balance — so `tests/backtest/test_split.py` compares per-trade `lot_size` against a standalone run of the same signals.
- **A positional slice of the signal list looks like a time split on every run that comes from the DB (2026-09-15).** `repo.fetch_backtest_signals` ends in `ORDER BY created_at`, so slicing by index and partitioning by `created_ts` agree for every DB-sourced backtest and disagree only once a manual signal is added. A test that does not shuffle its fixture cannot tell the two apart. `split.partition` sorts by `created_ts`; tied timestamps are walked past rather than cut through, so two signals created at the same moment — which the tuner necessarily saw together — cannot land on opposite sides of a line that claims to separate what it saw from what it did not, and the *achieved* fraction is reported instead of the requested one.
- **A refusal and a thin side are two different silences, and neither is a split.** A template the walk refused (`unsupported_reason`) gets no split at all: halving nothing produces two rows of zeros where one already read as an argument for the template that was never run. A side below `split.MIN_TRADES_PER_SIDE` reports its trade count and a note instead of a win rate, on the same reasoning. The minimum is provisional and is an open owner decision — `docs/simon-handover/036-how-few-trades-is-too-few.md` — so it is named on screen rather than buried in the constant.
- **`stopped_after_tp` is invisible in win rate.** A trade trailed out after TP1 is a WIN, so a win-rate column cannot show a ladder being cut short. That is why `get_strategy_ladder_reach` reports it separately; the same blind spot is why the truncation went unnoticed for a month.
- **The trail-vs-ladder rule must not live inside the volatility block.** It first did, and vanished exactly when the bridge was down — which is when the model is guessing most. It is a statement about the templates' own geometry, always available, so it is unconditional. Caught by a test, not by review.
- **A "moved verbatim" extraction can leave its module-scope dependencies behind.** `analytics/ai_analysis_repo.py` was drained out of `frontend/pages/ai_trade_analysis.py` and arrived without `datetime`/`timezone` or the `_TP_HIT_RE`/`_SL_HIT_RE` patterns its functions use, so `_session_from_ts` raised NameError on every signal with a `parsed_at` and the claimed-TP scan raised too. It imported cleanly and wired up fine; nothing failed until it ran. Found 2026-08-26 by pyflakes, fixed, and pinned by `tests/core/test_ai_analysis_repo_names_resolve.py`. The page keeps its own copies of both patterns and a test asserts the two agree.

- **A closed trade now carries the session it closed in, and it is attributed in Python (2026-09-22).** `trade_table` rows gained `session` — `"asian" | "london" | "overlap" | "ny"`, or `None` when the broker's record has no close time — so the Analysis calendar's day view can total a day by market as well as by signal source, the way the NiceGUI page's "By Market Session" table did. Three things about it are deliberate. **The boundaries are `analytics.pnl.session_for_hour`**, the same ones the hourly heat map reads; a copy of them in TypeScript would be a second answer to where London ends, and this app already has four disagreeing definitions of a session (`docs/todo/bugs/057`). **The broker offset comes off before the hour is read**, through the same `BROKER_OFFSET` that decides which calendar DAY a trade is filed under, so a trade can never land on Monday by one reading of the clock and in the Asian session by another. **A missing stamp yields `None`, never a default** — bucketing it into whichever session contains hour zero makes a quiet session look busy, and the day view reports the count it could not place instead.


- One deliberate exception to no-writes: `get_orb_target_multiple` memoises its backtested multiple into `app_config` so the 25-day backtest runs once per day.
- `signal_lab_repo.py` exists as a named adapter because `db_module.db()` resolves to whatever `init()` last pointed at — folding those queries into the main repo would silently read the *trading* DB.
- The `except Exception: pass` around each source in `ticket_maps.py` is deliberate swallow-and-degrade — one missing source must not blank the others.
- `edge_stats.py` returns empty results for a missing engine DB (engines are optional installs); its profit-factor clause keys on `outcome IN ('win','loss','be')` to exclude open trades.
- `model_refresh_loop.is_running` is a **callable**, not a bool — a captured value would leave the loop spinning after shutdown.
- `scheduler.py`'s ORB time gate uses `Europe/London` while the daily/weekly gate uses bare server-local `datetime.now()` — recorded as pre-existing and preserved.
- `signal_extractor.extract_signal()` runs only after a deterministic parser fails, and only yields a trade with model confidence AND complete real price levels.

- **The Analysis trade table's Spread and Cost come from `trade_spread_cache`, and only `analytics/entry_spread.py` writes it (2026-09-29).** The NiceGUI History page looked up each uncached ticket's entry tick on refresh; the React port kept the read and dropped the write, so every trade from 2026-09-21 showed no spread and a zero cost (306 of 750 rows in the 30-day window). Restored inside `trade_table.closed_trades`, through the facade's existing `get_ticks_range` (first tick in the minute from the entry, which is what the bridge's `/tick_at` returns) rather than a new `get_tick_at` facade method. At most 40 lookups per read, newest first, so a cold cache fills over a few polls instead of stalling one. The table polls every 15s (was 60s, "really slow to update"). Pinned by `tests/services/analytics/test_entry_spread_backfill.py`.

## The ORB's auto-execute, measured (2026-10-01)

Owner: ORB "maybe one or two" setups. Replayed on the bridge's own gold
candles (M5 Oct 2025 - Sep 2026, M15 Dec 2023 - Sep 2026; scripts not kept,
the numbers are here):

- **The London report could not take its own trade.** It confirms a
  direction only once price clears the whole Asian range, and puts the target
  one 15-minute-range height past that range's edge. By then price is usually
  past the target, so `orb_auto_execute` skips the day as stale -- and the
  scheduler marks the day done anyway. Live log, 10-30 Sep: 7 stale skips,
  3 broker errors (each also burned the day), 3 fills. The fills entered at
  the market with the edge-anchored target: 2026-09-11 bought 4362.35, stop
  4353.01, target 4362.47 (9.34 risked for 0.12).
- **London breakouts on gold do not pay.** Every London variant (15/30/60-min
  range, midpoint or far-side stop, 1-3R) lost or broke even; 60-min with a
  midpoint stop lost 0.16-0.24R a trade in every year.
- **New York with the H4 trend does.** Split by H4 close vs EMA50, breakouts
  with it won and breakouts against it lost, in every variant, both data sets.
  `orb_ny.py` replayed through its own `evaluate`, minute by minute: M5 96
  trades, 45% at 2R, +0.34R a trade, every quarter positive; M15 (coarser, 2.8
  years) 183 trades, +0.15R, 2024 -0.07R. About two trades a week. Median
  risk about $21 at gold 4,100-4,400.
- **Auto-execute sat behind the email check.** `email_scheduler_sweep` returns
  before the ORB section on a node with no email provider, so such a node never
  auto-traded ORB. The New York path runs before that check.

`orb_ny_mode` defaults to 0 (golden rule 3), which leaves the London path
byte-identical -- the scheduler and report characterization tests pin it. At 1
the London report is email-only and `_run_orb_ny_auto` decides once a minute
between 10:00 and 11:40 New York: a signal goes to the unchanged
`orb_auto_execute`, a skipped or missed day is logged as `[ORB NY] no trade
today: ...`, and both set `orb_auto_execute_last`, the same key, so a mode
switch mid-day cannot trade twice. An attempted order that errors still ends
the day: a ReadTimeout is an unknown outcome and a retry could double-fill.

**`/candles_range` stamps drift in a quiet market.** The bridge converts to UTC
with `tick.time - time.time()`, unrounded, so when the last tick is stale the
stamps come back seconds late (D1/M30 fetched at 21:57 UTC came back 13-47 s
off). `orb_ny.build_report` rounds to the minute. The bridge itself is not
changed here; it runs on the VPS.

## Open questions

- The scheduler London-vs-server-local timezone inconsistency is preserved, not resolved.
- The `core_orb_report` split is still pending (auto-execute moves with the trading surface).
