# Trading

**Living file — update when this domain teaches you something.**
Covers: `backend/src/services/trading/`, `dpm/`. **Every file here is a
real-money surface — read `docs/system/rules/20-trading-safety.md` and the
`/safe-change` skill before touching anything.**

## What it is

Every real-money surface in the app: market and limit order placement, the
close and partial-close paths, manual orders from the UI and from bot
commands, lot sizing and fee/P&L math, profit reconciliation against MT5,
Instant Market Entry (IME) and its follow-up watchdog, and the
auto-execution flow that turns a parsed Telegram signal into a live order.
Orders reach MT5 through the Python bridge or the EA bridge; most modules
here do not call the broker themselves — they call whatever `bridge` the
caller supplies.

DPM (Dynamic Position Management) is the adaptive in-trade manager: it
computes trail distance, breakeven trigger and TP1 close-% live from
ATR/ADX/session/momentum, and self-calibrates those multipliers from
closed-trade outcomes.

## Where the code lives

- `services/trading/open_trade.py` — the actual placement (Python bridge or EA path); holds the EA-ladder close-% tables (`_CLIMBER_PCTS`, etc.)
- `services/trading/open_from_signal.py` — back half of `open_trade_from_signal`: atomic signal claim, the `open_trade` call, strategy-specific post-fill `modify_order` overrides
- `services/trading/close_trade.py` — `close_trade` / `record_close` / `close_all_ladder_legs` / `get_trading_balance`, plus `CloseTradeContext` — **the frozen close path**
- `services/trading/partial_close.py` — DB-side bookkeeping *after* a broker-side partial close; never calls the bridge
- `services/trading/scan_auto_execute.py` — the auto-execution flow; "highest real-money surface in the whole migration series"
- `services/trading/instant_entry.py` / `instant_followup.py` — IME opening flow (provisional ATR-derived stop) and follow-up SL/TP application + timeout watchdog
- `services/trading/limit_order_signal.py` — Limit Runner: the only strategy placing a genuine broker-side pending limit, via the EA
- `services/trading/manual_limit_order.py` / `manual_market_order.py` — the UI's manual order backends
- `services/trading/orb_execute.py` — ORB/IVB morning auto-execute as an EA pending limit at the reload zone
- `services/trading/update_signal.py` — signal edits propagated to any linked live trade
- `services/trading/profit_sync.py` — reconciles P&L against MT5 deal history; residual-position sweep
- `services/trading/fees_sizing.py` — `calculate_fees`, `pnl`, `suggest_lot_size`
- `services/trading/ai_signal_fallback.py` — last-resort AI extraction; `apply_sl_adjustment` is a real `modify_order`
- `services/trading/bot_trading.py` — `/close`, `/activate`, `/marketbuy`, `/marketsell`, `/report`
- `services/trading/trade_repo.py` — the shared write-kernel SQL for the trade/signal/partial/pending/sim-account tables
- `services/dpm/engine.py` — pure adaptive-parameter computation + `run_calibration`
- `services/dpm/handler.py` — the live DPM handler (`bridge.partial_close` / `bridge.modify_order`)
- `services/dpm/bookkeeping.py`, `repo.py`, `performance.py` — `DPMCache`, milestone recording, DPM SQL, analysis-page reads

## Constraints / must not change

- **The close path is frozen** (`close_trade`, `record_close`, `_make_close_trade_ctx`, `partial_close_trade`): moved verbatim only, never reshaped, without owner sign-off and a demo session.
- Modules place no order themselves — they call whatever `bridge` the caller supplies. Never reach for a global bridge.
- `partial_close.py` must never call the MT5 bridge — the broker-side partial happens at the strategy-handler call site before it runs.
- Limit Runner and ORB have **no Python-bridge fallback**: if the EA is unavailable the signal is *skipped with an explanatory skip_reason*, never silently executed under a different model. `manual_limit_order.py` inherits the same rule.
- `update_signal.py`: Conservative, Conservative Trial and Scalp Runner compute their own fixed SL/TP1/TP2 from actual fill price; a follow-up signal **must not** overwrite those with the channel's ladder (confirmed live on ticket 1556670216).
- `orb_execute.py`: the scheduler runs on both nodes but only the *active trader node* may act — otherwise the same trade fires twice.
- **Max Open Trades counts SLOTS, not open rows** (owner, 2026-09-04): an open
  position, an order resting at the broker, or an open in flight each hold one,
  from the moment the order exists until the position it becomes is closed.
  One definition, `signal_state_repo._SLOTS_IN_USE_SQL`, used by the claim, the
  Reversal Engine's own claim, `open_trade`'s backstop and every pre-check;
  `count_slots_not_yet_open()` is the half for callers that already hold an
  open-trades list. `open_trade` passes `exclude_signal_id` because the claim
  runs FIRST for the same signal -- without it the backstop refuses every trade
  the normal path makes. Before this, a resting order held no slot at either
  end and N resting orders became N open trades over any cap.
- `trade_repo.py`: multi-row writes are one `transaction()` — both rows land or neither does.
- **`apply_full_close` returns whether THIS caller closed the trade, and the five callers that alert on a close must check it (2026-09-04, live ticket 1940612275).** The compare-and-set (`WHERE trade_id=? AND status='open'`) has protected the *database* from a duplicate close since stage1 phase2/040, but it returned `None`: `record_close` never read the rowcount and handed every caller a full-looking result dict, so the loser of the race believed it had closed the trade and announced it on Telegram. The owner got one stopped-out EA Template trade reported twice, seconds apart. `record_close`'s result now carries `already_closed`, and `reconcile_sl_hit`, `check_profit_close_target`, `close_trade`, `position_sync`, `ea_bridge._on_trade_closed` and the placeholder repair each go quiet when it is set. Pinned by `tests/trading/test_close_alert_not_sent_twice.py`.
- **A close-path block either RECORDS an outcome or RE-EVALUATES one, and only the first kind may be gated on `already_closed` (2026-09-04).** Everything after the compare-and-set in `record_close` used to run for both racing callers. Three of those blocks record: `push_trade_closed` (its upsert is keyed on `(node_id, trade_id)`, and the loser's `gross_pnl` is 0 because the winner zeroed `remaining_lots` — so `outcome` graded as `"be"` and overwrote the real win or loss, which is the column the Edge Dashboard's win rate reads), `record_live_trade_outcome` (one loss counted as two consecutive losses; at the default threshold of 3 that lets two real losses halt live execution for the cooldown, and tripping RESETS the counter so the evidence deletes itself), and `finalize_dpm_record`. Those three are now `if won`. The Risk Governor, give-back guard and daily-loss ceiling are deliberately NOT: they measure a limit against the live balance and reach the same verdict twice, and skipping a protective check to tidy up a duplicate trades a real risk for a cosmetic one. Pinned by `tests/trading/test_a_lost_close_race_books_nothing_twice.py`.
- DPM's trade-table SQL deliberately *mirrors* `services/positions/repo.py` rather than importing it — a service's repo is private to that service.
- `dpm/engine.py` hard bounds are safety rails for XAUUSD: trail 2.0–50.0, BE 2.0–30.0.
- `is_active_trader_node` is always passed in as a pre-computed bool, never recomputed locally.

## Known things & gotchas

- **The header's NEWS box is keyed to `check_news_blackout()`, not `get_current_event()` (2026-09-04).** They answer different questions and the difference is wrong in both directions. `get_current_event` reports "are we inside a calendar window" and **never consults the `enabled` flag** — `is_high_impact_window` and `check_news_blackout` both do — so a badge driven by it announced a news pause while the blackout was switched off and orders were still going out. In the other direction, when the feed is unreachable and nothing was ever cached, `check_news_blackout` falls back to `_hardcoded_fallback` and really does block entries while `get_current_event` returns `None`: trading paused for news with nothing on screen saying so, which is the case the box exists for. `news_calendar.news_pause_state()` composes the two — the blackout call decides visibility, the event only supplies the name and resume time — so "box visible" and "orders held for news" are one fact rather than two implementations that have to agree. The same fix stopped the Telegram "NEWS EVENT — Trading Paused" alert firing when no pause was in force. Pinned by `tests/utils/test_news_pause_badge_state.py` (moved out of the closed `tests/core/` when the header countdown landed), which also covers the `resume_ts` the badge counts down against.
- `dpm/handler.py`: `partial_close_trade` has its own "move SL to breakeven on TP1" logic, making the handler's own move redundant — **except** when `tp1_partial_pct == 0`, where the handler's move is the only mechanism.
- `scan_auto_execute.py` returns `followup_matched: True` on the IME-followup early return; without it a caller can't distinguish that case from a normal open (both return `executed: True`) and would send a duplicate Telegram alert.
- **The IME follow-up matcher is age-bounded, and must stay that way (2026-08-28, live miss).** `trade_repo.find_latest_instant_trade` was bounded by nothing but `status='open'`, `tg_source` and (in the caller) direction, so *any* open same-direction trade on a channel absorbed the next full signal as its "follow-up" — indefinitely. On Gold Diggers VIP a bare BUY trigger opened a trade at 11:41 and took its genuine follow-up 37s later; 28 minutes on, an unrelated BUY signal (tg_id 19832, zone 4592-4596) matched the same still-open trade and was consumed. Because `scan_auto_execute` returns before the execute/queue branch on `followup_matched`, that signal opened nothing, queued nothing and hit no filter — the news blackout was off and 81 minutes away, and never reached. The trade was `managed_by='ea'`, so its levels were discarded too: the signal produced literally nothing. Both copies of the query are now bounded by `ime_followup_timeout_s`: `trade_repo.find_latest_instant_trade` takes a required (never defaulted) `open_since`, and `cluster/sync_repo`'s twin — the forwarded-follow-up path used under centralized signal generation — computes the same cutoff itself, having exactly one caller. Fix the local copy without the VPS one and the bug simply moves nodes. **`tp1 IS NULL` is not an adequate bound**: `ime_timeout_watchdog` deliberately skips EA-managed trades, so those keep `tp1` NULL for life and would stay eligible forever. Pinned by `tests/core/test_instant_followup_staleness.py`.
- **The `tp_open`-not-None guard on the IME follow-up check must agree with `scan_messages.py`'s template-override check, or a template channel double-trades (2026-09-03, live).** `execute_auto_signal`'s follow-up gate (`scan_auto_execute.py`) used `parsed.get("tp_open") is None` alone to mean "this is a genuine standalone Limit Runner pending-order signal, never a follow-up" — a rule added 2026-07-24 to stop a real Limit signal for a *different* setup being swallowed as a follow-up to an unrelated open trade. But `tp_open` gets set (to `True` or `False`, never absent) by `parse_limit_order_signal` for *any* text matching `BUY|SELL (LIMITS )?GOLD @` — "LIMITS" is optional in that regex — which is also exactly how GOLD DIGGERS INSTITUTIONAL phrases its own ordinary follow-up ("BUY GOLD @ 4482/4481 ... TP OPEN ... SL 4480"). That channel is bound to an EA Template, so `scan_messages.py`'s own routing (`tp_open is not None and not is_template_override(strategy)`) already never sends it down the real Limit Runner order path — the resting order the 2026-07-24 fix protects doesn't exist on this route — but `execute_auto_signal` didn't know that and blocked the follow-up match anyway. Two real MT5 orders, 11 seconds apart, for one signal. Fix: `execute_auto_signal` now computes the same `tp_open is not None and not is_template_override(strategy)` condition scan_messages.py uses, so a template channel's Limit-shaped follow-up still reaches the matcher. Pinned by `tests/core/test_scan_messages_auto_execute_surface.py::test_ime_followup_is_checked_for_limit_shaped_signal_on_a_template_channel`.
- Limit Runner Entry Realignment (off by default): if price already moved through the zone, a resting limit at the near edge is an invalid broker price (root-caused live 2026-07-23). When enabled it enters at market and shifts SL/TPs by the breach delta, preserving R:R.
- Pending-order expiry differs by strategy: 240 min for Limit Runner / manual limit; 60 min for ORB.
- `profit_sync.py` falls back to scanning 90 days of deal history when per-ticket history returns nothing; sums profit + swap + fee.
- `calculate_fees` applies swap only when `hold_hours >= 24` (whole nights).
- `close_trade.py` takes `_schedule_profit_sync` / `_background_close_commentary` as injected async callables defaulting to no-op — close behaviour is testable without the retry loop or notifier.
- DPM calibration is trusted only after a minimum trade count per (session, momentum) group; calibrated multipliers live in `app_config`, refreshed at most every 10 minutes.
- `dpm/performance.py` failures collapse to `[]` deliberately — the intended degraded state on a fresh install.

## Open questions

- The pre-fill entry-mid fallback values recomputed in `signals/resolution.py` are described as "dead in practice" — retained but unverified as removable.
- The TP-ladder close-% tables in `open_trade.py` are out of scope for live tuning — "retuning those is a materially bigger, separate task."
