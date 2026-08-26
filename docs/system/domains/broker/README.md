# Broker

**Living file — update when this domain teaches you something.**
Covers: `backend/src/services/broker/`, `services/health/`,
`mt5_bridge.py` (repo root), `mql5/`.

## What it is

The only code allowed to talk to MT5 — either through the `mt5_bridge.py`
HTTP subprocess (Mac, MT5 under Wine) or in-process on native Windows — and
to the companion MQL5 EA over a local TCP socket. It also owns credentials,
account facts (deposits, fees, performance), deal-history backfill, and the
watchdog/self-healer machinery that restarts a dead bridge and reclaims
EA-managed trades. Python is the "brain" (parsing, ML scoring,
direction/entry/SL/TP); the EA is the "hands" (broker-side SL/TP and
per-tick trail/partial ladder inside MT5's `OnTick`). Everything is
127.0.0.1-only.

## Where the code lives

- `mt5_bridge.py` (repo root) — the Wine-Python subprocess serving `/health`, `/tick/XAUUSD`, `/candles`, `/account`, `/positions`, `/history`, `POST /reconnect` on port 9000 (stdlib + `MetaTrader5` only)
- `services/broker/mt5_client.py` — async HTTP client for the bridge; all methods return None/empty on failure rather than raising
- `services/broker/mt5_native.py` — `NativeMT5Bridge`: imports `mt5_bridge.py` as a module on native Windows, same public interface, no HTTP
- `services/broker/bridge_process.py` — launching/recovering the bridge subprocess; `bridge_script_path()` pins the repo-root lookup
- `services/broker/watchdog.py` / `watchdog_loop.py` — single-cycle bridge health check + the 60s loop shell
- `services/broker/ea_bridge.py` — newline-delimited-JSON TCP link to the EA; `is_ea_healthy()`, `push_global_config()`, per-strategy dispatch decisions
- `mql5/ForexTraderBridge.mq5` — the EA: receives a fully-decided trade, places it with real broker-side SL, manages trail/partial ladder in `OnTick`
- `services/broker/ea_templates.py` — EA-native trade-management templates selectable per channel via `template:<name>` override
- `services/broker/repo.py` — broker SQL, including the pending-order fill/cancel lifecycle as single transactions
- `services/broker/credentials.py` / `credentials_repo.py` — MT5 credential storage (always in the demo DB, env-independent) and bridge credentials-file sync
- `services/broker/history_import.py` — backfill of closed positions from MT5 deal history; never sends anything to MT5
- `services/broker/fake_bridge.py` + `fake_market.py` — debug-mode stand-in (2026-08-11): full duck-typed surface of both clients (introspection-pinned), deterministic closed-form price curve / JSON scenarios (`tools/debug_scenarios/`), in-memory ledger with SERVER-SIDE SL/TP settle (the monitor defers a local SL crossing to the broker — a fake without settle deadlocks that path), `inject_error()` for rejection testing. No network code. NOT wired into `_make_bridge` — that 3-line seam edit is Simon-gated (local-debug-mode 020)
- `services/broker/deposits.py`, `mt5_performance.py`, `fees.py` — net deposits (1h cache), deal-history performance stats, platform-fee rate
- `services/health/self_healer.py` — 90s log scan for known recoverable patterns; restarts bridge / reconnects Telegram / restarts the monitor
- `services/health/log_events.py` — live-diagnostics feed of meaningful log events

## Constraints / must not change

- `broker/` is the only package allowed to talk to the MT5 subprocess and the EA.
- The watchdog never places/closes/modifies an order — it only reads health and toggles AutoTrading / restarts the bridge.
- `history_import.py` is backfill only — never sends anything to MT5.
- All native MT5 calls are serialized through a single `asyncio.Lock` (the `MetaTrader5` package is not documented as thread-safe).
- The EA and Python always run on the same machine; only 127.0.0.1 is ever used.
- DPM trades are never handed to the EA — DPM's continuous recomputation has no MT5-native equivalent.
- `is_running` and `get_inhibit_reconnect` in the watchdog loop must stay **callables** — a captured bool would keep the loop spinning after shutdown, or restart a bridge the user deliberately stopped.
- `bridge_script_path()` exists because the old inline `".."` walk failed *silently* (bridge never restarts); depth is pinned by `tests/core/test_bridge_process_relocation.py`.
- One-time MT5 terminal setup: Tools > Options > Expert Advisors must allow socket for 127.0.0.1, or `SocketConnect` always fails.

## Known things & gotchas

- `services/broker/debug_guard.py` (2026-08-11, review C1): `TradingRuntime.__init__` passes `_make_bridge`'s pick through `reject_real_bridge_in_debug()` — a debug-mode boot that selected a real bridge class raises instead of logging into a live account behind the "no real orders" banner. **Consequence: a plain `FOREX_DEBUG_MODE=1` boot refuses until the Simon-gated seam wires the fake** (local-debug-mode 020); once the seam lands the guard simply never trips (pinned by `tests/runtime/test_debug_bridge_guard.py`). Bridge *selection* in `_make_bridge` is untouched.
- Timing constants: watchdog checks every 60s, restart cooldown 180s, startup wait 20s. Self-healer: 90s poll, 300s window, threshold 3 occurrences — deliberately below the watchdogs so it ignores one-off blips.
- Watchdog state is a caller-owned dict mutated in place; callers must seed it with the original loop's initial values.
- `watchdog.py` imports `monotonic` by name so tests can pin it — the real clock made the cooldown test pass only on machines up longer than the cooldown.
- EA heartbeat timeout is 8.0s; on EA silence, Python's handlers take management back for any trade marked `managed_by='ea'`.
- Every EA template field is re-sent on each open/pending call, so changing template values never requires an EA recompile.
- Global harvest config is checked against **every** open position on the symbol each tick — unlike the old per-template flag.
- **The EA harvests on EITHER trigger:** `if(profit >= tplHarvestThreshold || pipsHarvest)`. `harvest_pips` is therefore not a refinement of `harvest_threshold`, it overrides it — any positive value closes the trade at that many pips and the dollar threshold can never be reached. It shipped as **1.0** (migration 17's column default *and* `ea_templates.DEFAULTS`) while the EA implemented it assuming "0 = off, matches every template saved before this existed"; nothing had ever held 0, so an opt-in feature was live on every template. Found 2026-08-26 when a template set to harvest at $30 closed two trades at $1.40 each. Default is 0.0 now and migration 29 clears the 1.0 rows. **`harvest_pips` is still not exposed in any UI** — a user cannot see or change it.
- Lesson worth keeping: an EA-side default (`TplD(..., 0.0)`) is not the system's default. The value that reaches the EA is whatever the DB column and `DEFAULTS` carry, and those are set independently. When wiring a dormant template field, check what the existing rows actually hold before assuming the fallback applies.
- `mt5_client.py` deliberately promotes *sustained* tick failures from DEBUG to WARNING with wording the self-healer's regex matches.
- Tick cache TTL 1.0s; candle cache TTL 5.0s.
- MT5 candle timestamps come back on the broker's UTC+3 offset; signal timestamps are true UTC.
- In demo mode MT5 charges no commission, so the platform-fee rate is estimated from settings; live ECN uses the real deal `fee` field.
- `ForexTraderBridge.mq5` carries an explicitly **temporary** diagnostic heartbeat for the `scalp_runner` orphaned-trade investigation.

## Open questions

- The `scalp_runner` "silently orphaned trade" investigation is still open — the EA's diagnostic heartbeat is marked "remove once closed".
