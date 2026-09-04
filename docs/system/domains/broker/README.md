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
- **Every change to `mql5/ForexTraderBridge.mq5` bumps BOTH `EA_VERSION` and
  `EA_VERSION_DATE`, in the same edit — and `#property version` beside them,
  which MQL will not let a `#define` stand in for.** They are the only way to
  tell a running build from a current one. `__DATETIME__` says when the `.ex5`
  was COMPILED, which says nothing about how old the source behind it was:
  compiling a three-week-old file stamps it with today. The version says what
  the source is, the date says when it last changed, and the pair is what the
  handshake (`ea_bridge/_version.py`, which greps `EA_VERSION` out of the repo
  copy on every connection) compares. A change that skips the bump makes the
  app report a stale terminal build as current — the exact failure documented
  at the top of `tools/deploy_ea.sh`, which cost a day of correct fixes
  against a build from three weeks earlier. Deploying is still two steps:
  `tools/deploy_ea.sh`, then compile in MetaEditor (F7).
- One-time MT5 terminal setup: Tools > Options > Expert Advisors must allow socket for 127.0.0.1, or `SocketConnect` always fails.

## Known things & gotchas

- **The running bridge process can be older than `mt5_bridge.py`, and it fails
  by 404 rather than by looking broken (2026-09-04, live).** The bridge had
  been up since 16:28:08 on 09-03; `/ticks` was added at 16:38 that same day
  in c919996. So `/tick`, `/candles`, `/account` and `/health` all answered
  200 while `/ticks` answered `{"error": "Unknown path"}` — a bridge that was
  healthy by every check the app makes, and stale in exactly one endpoint.
  `get_ticks_range` swallowed the 404 and returned `[]`, which the backtest
  page rendered as "No ticks returned — ensure the bridge is connected", and
  the diagnosis went to the Days field and the one-day range cap instead. A
  404 on a bridge endpoint has one cause — the process predates the endpoint —
  so `mt5_client.get_ticks_range` now raises with that diagnosis and the
  remedy (restart the bridge). Every other transport failure keeps the quiet
  empty list: this feeds a page button, not a trading path. **When adding a
  new bridge endpoint, remember the bridge must be restarted to serve it**;
  the app's own health check will not notice. Pinned by
  `tests/services/broker/test_ticks_range_stale_bridge.py`.
- **A `trade_closed` with no `close_price` is an observation, not an exit
  (2026-09-04, live, ticket 1935433548).** `HandleRestoreTrade` is the only
  sender that omits the field: on "hello" the app pushes its open EA-managed
  rows back with `restore_trade`, and a ticket the EA cannot select as a
  position is reported as `{"reason":"closed_while_disconnected"}` and nothing
  else. `float(msg.get("close_price", 0))` read that absence as an exit at
  $0.00, so `record_close` computed `(0 - 4478.35) x 0.1 x 100` =
  **-$44,783.50** on a 0.1-lot trade and wrote it to `net_pnl`,
  `realised_pnl` and `vantage_simulation_account.balance` -- then fed it to the
  daily-loss and give-back guards, which halt trading for the day. Telegram
  announced the loss. The broker had no closing deal for the ticket at all,
  which is why the History tab (built from MT5 deal history, not from DB rows)
  never showed the trade -- **absence from History is itself evidence that no
  broker close exists**. `_on_trade_closed` now asks the broker
  (`_broker_exit_price` -> `get_position_history`) whenever the price is
  missing or zero, uses the last closing deal's price, and records nothing at
  all when there is no closing deal: "the EA cannot see the ticket" and "the
  position is gone" are different facts, and only the broker settles the
  second. Pinned by `tests/services/broker/test_ea_close_without_a_price.py`.
  **Still open:** `record_close` itself has an `entry_price == 0` guard but no
  `close_price == 0` guard -- it is the frozen close path and wants owner
  sign-off plus a demo session.
- **The EA reaches remote machines on its own now (2026-09-04):
  `services/broker/ea_deploy.py`, called from `apply_update` once the pull has
  landed.** The pull already carried `mql5/ForexTraderBridge.mq5` to every
  machine -- it is in the repo -- but nothing copied it into a terminal's
  `MQL5/Experts`, so an EA change reached the app everywhere and the EA
  nowhere. `experts_dirs()` finds every terminal (Windows roaming profiles,
  CrossOver bottles, the MetaQuotes macOS wrapper), each copy is verified
  byte-identical and backs up what it replaces, and one unwritable terminal
  never costs the others theirs. It swallows its own failures by design: the
  app update has already succeeded by then, and a failed file copy leaves the
  machine exactly as it was.
  **The three limits are worth knowing before relying on it.**
  (1) *Compiling is Windows-only.* `metaeditor64.exe /compile` works
  headlessly there; under CrossOver it exits 0, writes no log and rebuilds
  nothing, so `compile_ea` refuses on macOS rather than reporting a build that
  never happened. Its exit code is never trusted anyway -- the check is
  whether the `.ex5` landed newer than the `.mq5`.
  (2) *The portable answer is to ship a compiled `.ex5` in `mql5/`* beside the
  source. Then no remote machine needs MetaEditor: the pull brings the binary,
  this drops it in, and the running EA picks it up. Compile once, in the same
  change that bumps `EA_VERSION`. A repo with no `.ex5` never deletes a
  terminal's locally compiled one.
  (3) *Attaching is not automatable* -- it needs a chart template at terminal
  start, there is no runtime API. **Restarting the terminal is the indirect
  route and is now wired up** (owner, 2026-09-04): MT5 restores its charts, and
  the expert with them. `core_ea_link_watchdog._maybe_reload_stale_ea` fires on
  the HEALTHY path -- the EA is alive and merely the wrong build -- with
  `ea_deploy.reload_decision` holding the guards: stale version *known* (None
  is not evidence), **zero trade slots in use** (a restart blinds management
  for ~2 min, which is cheap only when nothing is at risk; an unreadable slot
  count counts as busy), a bridge whose restart actually reloads the expert,
  not manually stopped, and **once per process** -- without that last cap a
  macOS machine that cannot compile reloads the same old `.ex5`, reports the
  same stale version, and restarts forever. Same lever the outage path has
  used since 2026-08-07, different trigger.
  **Two things still to verify on a live terminal**, neither checkable from a
  Mac: that MT5 reliably reloads a running EA when its `.ex5` changes on disk,
  and that an `.ex5` compiled on one terminal build runs on the others'.
  Pinned by `tests/services/broker/test_ea_deploy.py` and
  `tests/positions/test_app_update_deploys_the_ea.py`.
- **Global Harvest is a BASKET total, not a per-trade target (owner,
  2026-09-04, EA v1.06).** It used to close each position whose own floating
  profit reached the threshold, which is a different feature under the same
  name: six trades at $15 each is $90 of open profit that a $75 harvest never
  touched, and the on-chart panel's "at $75.00 profit per trade" was the only
  thing that said why. `CheckGlobalHarvest` now sums every position on the
  symbol and closes them ALL when the total is reached -- including any
  individually in loss, which is what banking a combined total means. Panel
  shows the live total against the threshold ("$12.40 / $75.00 combined (6)"),
  from the same `GlobalHarvestFloating()` the check uses, so the display cannot
  drift from the trigger. Account-wide mirror of the template-level
  `basket_harvest_threshold` (`core_equity_protect.check_basket_harvest`) and of
  `equity_protect` in the loss direction. **Not yet compiled or demoed.**
- **Secret files are restricted through `utils/file_perms.restrict_to_owner`,
  never `os.chmod` alone.** `chmod` does not restrict a file on Windows -- it
  toggles a read-only flag, the permission bits are ignored, and the file keeps
  whatever its parent directory grants. Four secrets were written that way and
  all landed `0o666` on Windows: the bridge credentials file (**the plaintext
  broker password**), the private CA key (**anyone holding it can mint a
  certificate the app trusts**), the credentials encryption key, and the
  licence. Found 2026-09-02 from a Windows CI run; fixed the same day once the
  owner confirmed Windows clients are in scope. The helper uses `icacls
  <path> /inheritance:r /grant:r <user>:F` -- **both halves matter**, since
  granting the owner without dropping inherited ACEs leaves everyone else's
  access untouched. It returns False rather than raising when it cannot apply
  the restriction, and every caller logs at ERROR naming what is exposed.
  `tests/utils/test_every_secret_is_restricted.py` fails if a new secret is
  written with a bare `chmod`. The real ACL is verified by a Windows-only test
  that runs on CI, because it cannot be verified on the Mac this is developed
  on.
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
