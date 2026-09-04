# Positions

**Living file — update when this domain teaches you something.**
Covers: `backend/src/services/positions/`, plus reconciliation code in
`broker/position_sync.py`, `broker/untracked.py` and
`trading/profit_sync.py`.

## What it is

Owns every open trade after it has been placed: a monitoring loop reads a
tick, dispatches each open trade to its strategy handler (13 `handle_*`
handlers), walks TP ladders, detects SL/TP crossings, and protects trades
with a safety-net sweep. It also reconciles the app's own trade records
against what MT5 actually still holds, and syncs realised profit back from
the broker's deal history. It never decides *whether* to trade — only how an
already-open trade is managed and closed. Much of this code was extracted
verbatim from the old `SimulationEngine`, so its structure is deliberately
shaped around "no behaviour change".

## Where the code lives

- `services/positions/monitor_cycle.py` — one pass of the monitor loop: tick, dispatch every open trade, pending-signal watcher, IME timeout watchdog, cadence counters
- `services/positions/monitor_loop.py` — the computational blocks: `check_sl`, `reconcile_sl_hit`, `check_profit_close_target`, `reclaim_ea_managed_trade`
- `services/positions/tp_tracking.py` — TP/SL trigger detection plus `TPCache` (2.5s triggered-TP cache, log throttles)
- `services/positions/tp_ladder.py` / `tp_ladder_loop.py` — shared TP-ladder walk and the sub-second (0.25s) fast poll that solely owns TP-crossing detection for ladder strategies when DPM is off
- `services/positions/safety_net.py` — periodic sweep moving SL to breakeven when the live loop missed it (1800s per-trade alert cooldown)
- `services/positions/max_tp.py` — post-close "highest TP actually reached" checker (read-only candles + DB writes)
- `services/positions/handle_*.py` — the per-strategy tick handlers (be_runner, conservative, conservative_trial, no_sl_scale/Trend Ratchet, orb_fixed, protected_scale, scale_out, scalp_runner, trail_stop)
- `services/positions/repo.py`, `ladder_repo.py`, `spread_cache.py` — position SQL, `vantage_ladder_legs` CRUD, cached spread cost
- `services/broker/position_sync.py` — reconciliation against broker-held tickets (`sync_closed_mt5_positions`, `PositionSyncCtx`)
- `services/broker/untracked.py` — live MT5 positions with no app trade record (`_untracked=True`)
- `services/trading/profit_sync.py` — realised P&L reconstruction from MT5 deal history

## Constraints / must not change

- Handlers modify no order themselves — they only call whatever `bridge` the caller supplies.
- `position_sync.py` is relocation-only: "this code decides that a real trade has closed, and reshaping it needs a demo-account session and sign-off."
- `MonitorState` and the `mt5_sync_missing_streak` / `miss_threshold` counters must be shared **by reference**, never copied — copying resets them each cycle, silently disabling MT5 reconciliation and making every transient broker hiccup read as a real close.
- `safety_net.py` must not touch broker-side SL for EA-managed trades (the EA owns them); `be_runner` is also skipped because it sets a real broker-side TP.
- Re-extraction of the handlers against `CloseTradeContext` is explicitly still gated.

## Known things & gotchas

- **A settings key that does not exist reads as None and fails silently.** `core_bot_panel` read `rs.get("ime_enabled")` in two places; the column is `immediate_market_entry`. `on = not bool(None)` is always True, so the Telegram panel could switch Immediate Market Entry ON and never OFF, and the System menu always displayed OFF. Found 2026-08-26, fixed 2026-08-27, pinned by `tests/core/test_bot_panel_actions.py`. This codebase has hit "IME cannot be turned off" before from a different cause (a backfill re-running every boot, see `tests/conftest.py`) -- when a control seems stuck on, suspect the read before the write.


- `reconcile_sl_hit` does not trust a local SL crossing: it checks MT5's live position volume first and returns `"deferred"` / `"partial"` / `"closed"` accordingly.
- **`check_sl` runs on EVERY open trade, including EA-managed ones — it sits at `monitor_cycle.py:210`, ABOVE the `managed_by == 'ea'` skip twenty lines below it.** So a template trade whose stop fires is detected here *and* by the EA's own `trade_closed` event, and the two race. `reconcile_sl_hit` only defers while the ticket is still fully open at the broker, which by then it is not. The database survives it (`apply_full_close` is a compare-and-set), but until 2026-09-04 both paths sent a Telegram close alert and the owner got the same close twice (ticket 1940612275). The alert now depends on `record_close`'s `already_closed` flag — see the trading domain. Reconciliation had already been given a narrower version of this fix in 2026-07 by excluding `managed_by='ea'` rows from its poll (`broker/repo.py::fetch_python_managed_open_trades`); the SL path was never covered by it.
- Close detection requires a **miss streak**: a ticket must be absent from MT5 for `miss_threshold` (default 2) consecutive cycles before the trade is believed closed.
- The monitor loop's sleep is adaptive (1s vs 5s) driven by `has_open_trades` / `has_pending_signals`, which deliberately keep their previous value when a tick comes back empty.
- Ladder strategies need the separate 0.25s fast loop because gold TP levels can sit ~1 point apart and a spike-and-reverse can cross several tiers between two 1s samples.
- `_tp_level_from_extreme` deliberately continues past a `None` TP mid-sequence so a gap cannot hide every level beyond it.
- `check_profit_close_target` uses **cumulative** P&L (realised partials + unrealised), not just unrealised.
- `reclaim_ea_managed_trade`: while the EA is healthy Python skips dispatch; if unhealthy it flips `managed_by` in the DB, alerts, and Python takes over the same cycle. Nothing is ever left with no manager.
- Profit sync falls back from per-ticket history to filtering 90 days of deal history; realised profit sums `profit + swap + fee`.
- `dpm_candles` is the one piece of state the cycle writes back onto the runtime rather than keeping locally — `open_trade_from_signal` and the scan context also read it.

## Open questions

- None currently flagged.
