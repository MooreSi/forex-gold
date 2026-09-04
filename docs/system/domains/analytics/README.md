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
- `services/analytics/orb_report.py` — ORB/IVB range detection, volume profile, backtested target multiple
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
- **A backtest row of zeros is not a result — it may be a refusal, and it has to say so (2026-09-04).** `30 TP1 SL50 and Trail` came back `0 trades / 0% / +$0.00 / PF ∞ / final $1,000.00` beside two templates showing 90 trades each. Nothing was wrong with it: the run was tick-based and the template uses `trail_mode=candle`, which the **tick** walk refuses on purpose (`CandleTrailLevel()` trails to the last 3 closed M15 candles; the tick walk has bid/ask and no candle series). The bar walk supports it, so the same template simulates on bars and is refused on ticks. `_simulate_template` catches `UnsupportedTemplate` and returns `None` per signal, and 90 `None`s aggregate into zeros. Returning `None` rather than approximating is correct and must not change — "a plausible number from a template the walk cannot model is worse than no number, because it would be used to choose what trades real money" — but zeros in a comparison table read as *traded nothing, lost nothing*, which beside a row showing 62.8% max drawdown is an argument FOR the template that could not be simulated at all. `StrategyStats.unsupported_reason` now carries the refusal and the table renders "NOT SIMULATED" with the reason. Deliberately narrow: a **built-in** strategy on ticks is not "unsupported" (it is simply not walked), and a **missing** template is a different problem again — labelling it unsupported sends the user to edit a trail mode on a template that is not there. Pinned by `tests/backtest/test_unsupported_template_reason.py`.
- **A strategy's configured TP ladder is not the ladder its trades climb, and the AI prompt must be told both (2026-09-04, live).** The AI Analysis tab recommended `GD VIP - Single` on the reasoning that its eight rungs (20/40/60/80/100/120/170/270 pips) would "capture a continued move" and "let profits run". Its own 30 days said otherwise: of 85 closed trades, 50 topped out at TP1-TP2, 3 reached TP5+, the highest rung ever reached was TP6 (so TP7 and TP8 have **never** been hit), and 62 banked a rung and were then stopped out. The cause is geometry, not luck — the trail arms at 40 pips (TP2) with a 50-pip distance, and H1 ATR that morning was 118-158 pips, so a 50-pip give-back is a fraction of a normal hour and truncates the ladder every time. `request_market_analysis` could not have known: its prompt carried bid/ask, spread, 20 M5 closes, session high/low and a direction word — **no ATR, no volatility measure at all**, and no history of where trades finish. It was handed specifications and asked to reason about behaviour, and produced a fluent, confident, false story. Fixed by feeding it H1/M15 ATR **in pips** (the same unit as every trail and rung, so no conversion stands between the model and the comparison) plus `get_strategy_ladder_reach`, and by an unconditional rule that a trail arming at or below an early rung makes the upper rungs decoration. Note the recommendation itself was not wrong — that template is the best performer on the book — but the reason was, and a wrong reason generalises wrongly. Pinned by `tests/core/test_market_analysis_volatility_context.py` and `tests/core/test_strategy_ladder_reach.py`.
- **`stopped_after_tp` is invisible in win rate.** A trade trailed out after TP1 is a WIN, so a win-rate column cannot show a ladder being cut short. That is why `get_strategy_ladder_reach` reports it separately; the same blind spot is why the truncation went unnoticed for a month.
- **The trail-vs-ladder rule must not live inside the volatility block.** It first did, and vanished exactly when the bridge was down — which is when the model is guessing most. It is a statement about the templates' own geometry, always available, so it is unconditional. Caught by a test, not by review.
- **A "moved verbatim" extraction can leave its module-scope dependencies behind.** `analytics/ai_analysis_repo.py` was drained out of `frontend/pages/ai_trade_analysis.py` and arrived without `datetime`/`timezone` or the `_TP_HIT_RE`/`_SL_HIT_RE` patterns its functions use, so `_session_from_ts` raised NameError on every signal with a `parsed_at` and the claimed-TP scan raised too. It imported cleanly and wired up fine; nothing failed until it ran. Found 2026-08-26 by pyflakes, fixed, and pinned by `tests/core/test_ai_analysis_repo_names_resolve.py`. The page keeps its own copies of both patterns and a test asserts the two agree.


- One deliberate exception to no-writes: `get_orb_target_multiple` memoises its backtested multiple into `app_config` so the 25-day backtest runs once per day.
- `signal_lab_repo.py` exists as a named adapter because `db_module.db()` resolves to whatever `init()` last pointed at — folding those queries into the main repo would silently read the *trading* DB.
- The `except Exception: pass` around each source in `ticket_maps.py` is deliberate swallow-and-degrade — one missing source must not blank the others.
- `edge_stats.py` returns empty results for a missing engine DB (engines are optional installs); its profit-factor clause keys on `outcome IN ('win','loss','be')` to exclude open trades.
- `model_refresh_loop.is_running` is a **callable**, not a bool — a captured value would leave the loop spinning after shutdown.
- `scheduler.py`'s ORB time gate uses `Europe/London` while the daily/weekly gate uses bare server-local `datetime.now()` — recorded as pre-existing and preserved.
- `signal_extractor.extract_signal()` runs only after a deterministic parser fails, and only yields a trade with model confidence AND complete real price levels.

## Open questions

- The scheduler London-vs-server-local timezone inconsistency is preserved, not resolved.
- The `core_orb_report` split is still pending (auto-execute moves with the trading surface).
