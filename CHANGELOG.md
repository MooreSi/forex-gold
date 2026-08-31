## Unreleased — Money-path hardening + a bug class closed (2026-08-28 → 2026-08-31)

**Nothing in the "money path" section below is verified against a broker.**
Every item there is written, tested and mutation-tested, and none is finished.
Finishing each one needs Simon at a demo terminal:
`docs/simon-handover/013-the-five-demos-runbook.md`.

### Money path — IMPLEMENTED, NOT SIGNED OFF (demo required for every item)
- **A slow EA no longer causes a second order.** When the EA's acknowledgement
  timed out, the fallback placed another order for a trade the EA may already
  have on the book — and the two send paths stamped different identifiers, so
  no check was even possible. Both now carry the trade id, and the fallback
  asks the broker before sending. On 2026-07-30 this turned five signals into
  roughly 133 opens (stage3/010).
- **A send that gets no answer parks the signal as `unknown`** instead of
  returning it to `pending`, which the scheduler re-activates every 20 seconds.
  A rejection still stays retryable — the broker saying "no" is information;
  a timeout is the absence of it (stage3/020).
- **A refused broker close no longer becomes a database close.** `success=False`
  was never checked, so a refused close was recorded at the app's own local
  price while MT5 still held the position — the app then stopped managing a
  live trade and booked a profit that never happened. Two existing tests had
  enshrined that behaviour and were rewritten (stage3/040).
- **Broker↔database reconciliation** reports positions the app placed and lost
  the row for, and rows with no broker record. Read-only, asserted so by a
  structural test. **The repairers are deliberately not built** — they would
  write, through the frozen close path (stage3/030).
- **Protective halt numbers now agree with each other.** The risk governor fell
  back to 20% for both the daily-loss and drawdown limits while the schema said
  3%/8%; all three sources now say 3%/10%. Both post-close halt checks are loud
  on failure instead of swallowed at debug level (stage3/050).
- **Closing a trade twice no longer pays out twice.** `apply_full_close` ran
  `balance = balance + ?` with no guard; it is now a compare-and-set that only
  an `open` trade satisfies (stage1 2/040).
- **The trade-slot cap is enforced inside the atomic claim**, closing a race
  where two signals could both pass the check. A stranded-claim sweep releases
  a claim abandoned by a crash, keyed on when the claim was made — keying it on
  the signal's age would have released in-flight opens and opened them twice
  (stage1 2/030).

### Fixed
- **A phantom trade held one of five trade slots.** A placeholder row with
  `grid_legs_total IS NULL` could never satisfy the existing expiry condition,
  so it sat open at a fabricated P&L indefinitely (docs/todo/bugs/016).
- **One malformed Telegram message produced 8,319 log lines**, rescanned
  forever (docs/todo/bugs/015 — the log half; the recording half is deferred,
  see the ticket).
- **Backtests scored every timed-out trade as break-even.** Seven of eight
  simulators marked a time-stopped trade out at its entry price rather than the
  price it was actually cut at, so every strategy's timeout behaviour looked
  free (docs/todo/bugs/017).
- **Four cluster modules referred to 15 names that did not exist** after being
  split out of the two cluster servers. `_read_changelog` runs on every
  successful remote client connection, so a client would have been welcomed,
  licensed, then dropped before it learned what version to update to; the LAN
  discovery beacon raised on its first iteration (docs/todo/bugs/018).

### Security
- **The cluster sync channel now authenticates the server it connects to.**
  `tls_util`'s docstring promised a fingerprint check against a function that
  did not exist anywhere in the tree, and nothing called `getpeercert()` — so
  the "TLS" was encryption without authentication. Trust-on-first-use pinning,
  with the token sent only after the pin verifies (docs/todo/bugs/014).
  The licence channel is deliberately NOT pinned yet: unlike sync it has no
  recovery route if a pin goes wrong.

### Gates
- **New: undefined names.** A name a module uses but never defines — the shape
  a file split leaves behind. Four bugs in this repo have taken it, all found
  by reading rather than by a test. `tools.checks all` is now nine checks.
- Test coverage where it was thinnest and matters most: the remote server's
  connection front door (registration, licence delivery, token auth) 45% → 65%,
  the update-application path 17% → 80%.

### Documentation
- `docs/guides/install-from-scratch.md` — Windows and macOS, with the failure
  modes taken from the real branches in `Setup & Start FOREX.bat`.
- `docs/simon-handover/013-the-five-demos-runbook.md` — the demo session, step
  by step, so it is a confirmation rather than an exploration.
- The split-file queue in `docs/system/rules/70-file-organisation.md` was badly
  stale — every frontend file it listed is a package now. Rewritten from
  measurement.

## Unreleased — Upstream merge + structural sweep (2026-08-25 → 2026-08-27)

Merges `MooreSi/forex` main into the refactored tree, then repairs what the
merge cost and finishes two structural gates.

### Fixed — money path (all UNVERIFIED against a broker; demo session required)
- **Harvest closed trades at the wrong threshold.** `harvest_pips` (pips) was
  being conflated with `harvest_threshold` (account currency), and shipped
  defaulted to `1.0` rather than off. Two live trades closed at ~C$1.40 against
  a $30 setting. Default is now `0.0` (off), with migration 29 clearing the
  stale `1.0` from existing templates.
- **The Telegram panel's IME button could turn Immediate Market Entry on but
  never off** — the two reads used different config keys, so the "off" write
  landed somewhere the "on" read never looked
  (docs/todo/bugs/012).
- **The anti-compounding revert in `PendingWatcher` had no test.** Its absence
  had previously walked one signal's stop 110 pips over 80 passes. Now pinned.

### Structural gates
- **SQL gate: 56 statements across 22 files → 0.** Every remaining inline
  statement moved into a repo, each behind a test written first and confirmed
  by mutation. Four byte-identical copies of `trade_repo.get_trade` and three
  of `broker_repo.fetch_working_pending_orders` were deleted rather than moved.
- **Coverage ratchet green again** — `runtime.py` 63.9% → 77.7%,
  `services/positions` 75.8% → 87.4%, `services/trading` 86.4% → 88.2%, with
  the floors untouched (docs/todo/testing/011).
- Import contracts, transaction boundaries and fixture-dedup back to baseline.

### Decomposition
- `ea_bridge.py` 1,947 → 715, split into `_events` / `_panel` / `_restore` /
  `_version` / `_ids`
- `core_bot_panel.py` 1,689 → 604; `settings.py` → 11 modules; `app.py`,
  `history.py`, `chart`, `telegram`, `reversal_panel`, `breakout_panel` split
- Two runtime loop bodies relocated into their services with 20 new tests

### Baselines raised (owner sign-off, 2026-08-27)
- `runtime.py` 1310 → 1509, `cluster/remote/server.py` 1196 → 1256,
  `cluster/sync/server.py` 1073 → 1085, `mt5_bridge.py` 1335 → 1344, facade
  method count 79 → 88. Each recorded in `structure_baseline.json` with its
  reason. Three other entries were **tightened** in the same pass —
  `db/database.py` had 794 lines of unnoticed headroom and was removed from the
  section entirely.

### Tooling
- Repo-wide scanners now exclude `.claude/` — an agent worktree is a complete
  second checkout, and one background task took four gates red with ~370
  phantom findings.

**Suite: 3,622 passing, 0 failures. `python -m tools.checks all`: 8/8.**

## Unreleased — Road to Handoff (stage-2 sweep, 2026-08-11)

### Onboarding & usability
- First-run **Start Here** checklist: live licence / MT5 / algo-trading / risk /
  Telegram / demo-mode status with "Fix this →" jumps; dismissable, reopenable
  from Help
- Header **Help "?"** button on every screen → Getting Started (daily routine +
  the existing Setup/Registration/Orchestration/Glossary guides, finally linked)
- Plain-language subtitles on all 10 tabs (as tooltips; names unchanged)
- Empty lists now say what to do next (Trading signals, Analysis periods)
- About reorganised into "Set up once / Every day"

### Foundations
- Schema migrations are now an ordered, numbered registry
  (`db/migrations.py`, 12 steps) with a per-step `schema_version` stamp;
  legacy database shapes are fixture-tested to upgrade losslessly
- One-off data backfills moved to named steps (`db/backfills.py`) that fail
  loud instead of `except: pass`
- Test-suite remediation: 13 assert-nothing test files deleted (populated
  twins verified) and structurally banned; coverage floors added for
  `services/broker` and `runtime.py`; test-layout hazards gated; 35 duplicate
  `fresh_db` fixtures consolidated

### Debug mode (offline demo)
- `FakeMT5Bridge` + deterministic fake market (scripted JSON scenarios under
  `tools/debug_scenarios/`), with broker-side SL/TP settlement and error
  injection — **not yet wired into the live bridge selection** (that 3-line
  seam awaits the owner's sign-off + demo session)
- Fake Telegram reader replays scripted signals through the real parser;
  alerts/bot/news/AI/email are all no-ops or canned in debug — a debug boot
  makes zero outbound requests
- Unmissable **DEBUG MODE** banner; offline end-to-end test proves
  signal → open → manage → close on the fakes

### Handoff
- `docs/simon-handover/readiness-checklist.md` — the readiness gate, honestly filled
- Decision queue grown and provisionally answered where safe
  (docs/simon-handover/, restructure + debug-mode QUESTIONS)

## v0.8.2 — ORB Auto-Execute Fix, R:R Tracking & Version Reporting

## v0.42 — ORB Auto-Execute Fix, R:R Tracking & Version Reporting

### ORB/IVB report
- Auto-execute now places a pending order that waits for price to retest the reported reload zone (60-minute expiry, Telegram alert if never retested) instead of firing an immediate market order — fixes large entry-price slippage vs. the reported plan
- Added Asian session range (high/low) and an untested-extension target to the report
- Report naming corrected to consistently say "ORB/IVB" instead of just "IVB"

### History
- Added R:R (reward:risk) column to Trade History, computed per trade from its own entry/stop/TP1 levels
- Fixed Max TP Hit not populating for trades opened by the other paired node — now falls back to the cross-node consolidated ledger

### Reversal Engine
- Recalibrated signal-scoring weights against a backtest of 767 real Gold Diggers VIP/GD2 signals — round-number levels (round_5 especially) weighted up, Asia range/swing levels weighted down to match measured real-world hit rates
- Fixed nightly research silently failing on high-message nights — AI call timeout widened from 60s to 240s, plus a dedicated failure-alert email

### App
- Fixed version reporting drift — Settings > Update "Installed Version" and the admin console's per-client version display now read the same canonical version source (VERSION file, derived from version_history.py) as the rest of the app, instead of a separately-maintained file that had fallen out of sync

## v0.31 — ML Engine Overhaul, Signal Bus & Silent Mac Launcher

### Signal generator ML engines (all three: Bounce, Breakout, Reversal Engine)
- Switched from binary win/loss classifier to R-multiple regressor (LightGBM/SGDRegressor)
- Gate threshold changed from probability > 0.5 to predicted R > 0.0
- Lot sizing now scales with predicted R-multiple (0.5× to 1.3×) when model is trained
- 3–4 new contextual features per engine: `news_proximity_norm`, `equity_drawdown_pct`, `concurrent_agreement`, `regime_score`
- Model version auto-increments on feature count change; stale models discarded on restart
- Online learner updated to SGDRegressor (Huber loss); training target is actual R achieved
- Consecutive-loss direction cooldown: suppresses a direction after 3 losses within 2 hours
- ML metrics panel updated: Brier/MCC replaced with Pred R / Act R display

### Signal bus (cross-engine awareness)
- All three engines write to shared `signal_bus` table on signal open
- Conflict suppression window reduced from 600s to 180s across all engines
- `is_still_open` flag added: bus entries clear immediately when the originating signal closes
- `close_bus_entry()` wired into all signal close paths (TP, SL, manual close)
- `get_concurrent_signals()` now filters to open-only entries
- Engines passively avoid holding opposite positions simultaneously

### Bug fixes
- Fixed `name 'now' is not defined` in `_check_outcomes()` — outcome loop was failing every 5 seconds in unattended mode, preventing signal closure and ML online updates
- Fixed `IndexError: list index out of range` on page load — Breakout and Bounce ML panels were indexing into empty `cumulative_brier` list after regressor switch
- Removed stale Brier/MCC/calibration UI from all signal panels; replaced with R-multiple metrics

### Mac launcher
- Added `FOREX Trader.app` — double-click to start without opening a Terminal window
- Compiled as a native AppleScript app (Mach-O binary); runs via Terminal's TCC file-access permissions, minimises Terminal window once server is ready
- Already-running detection: double-click opens browser tab directly when app is running
- First-run fallback: opens Terminal with `FOREX Start.command` for setup output

### Windows launcher
- Added `FOREX Start (No Window).vbs` — launches via `pythonw.exe` with no console window
- Already-running detection: opens browser directly if port 8888 is listening
- Restart loop (exit 42) handled in VBScript; first-run falls back to visible setup window

## v0.23 — Remote Admin, Signal Generators & AI Analysis

- Remote admin system: WebSocket server on admin machine (port 8443, TLS encrypted)
- Admin panel in top banner: password-protected, shows connected clients with version and diagnostics
- Push updates to remote clients directly from the admin panel
- Remote client agent runs on all instances, auto-reconnects, applies pushed updates and restarts
- Settings > Update: shows app version, connection status, registration token, changelog
- Reversal Engine: reverse-engineers Gold Diggers VIP methodology (Asia range, swing levels, round numbers)
- Reversal Engine: ML correlation tracking vs real VIP signals
- Performance heatmap: AI analysis panel (Claude-powered, cached daily at 8am)
- AI trade analysis: TP1 = win evaluation, SL width analysis, entry drift = latency context
- AI trade analysis: signal generator development section (ML progress, self-learning assessment)
- Diagnostics: latency report for App→Bridge→MT5 and Telegram→execution paths
- Session label fix: Markets Closed shown correctly when enabled sessions are outside their hours
- Active trade duplicate badge fix (chart page and active trades tab)

## v0.22 — Bounce Generator & Real-Time Monitoring

- TEST tab renamed to Bounce Generator
- Signals section added to Trading > Strategy
- Bounce Generator live execution through full MT5 pipeline
- Real-time monitoring with 3-second velocity loop
- Outcome check interval reduced to 5 seconds
- Message detection latency reduced to ~18 ms

## v0.21 — Performance Improvements

- MT5 bridge persistent HTTP connection (~1.5ms round-trip)
- Adaptive monitor loop: 1-second polling when trades open
- Pre-fetch tick before order placement
- Telegram latency improvements (ConnectionTcpAbridged)
- TG Learning mode for signal scoring

## v0.2 — TEST Module (Claude Bounce Generator)

- TEST tab with isolated Claude-powered signal generator
- Virtual trading engine with 2% risk sizing
- Isolated database (test_signal.db)
- Post-trade learning notes and batch pattern analysis
- Circuit breaker after 3 consecutive losses

## v0.11 — Bug Fixes

- Fixed MT5 broker UTC+3 timestamp offset
- Signal parser edge cases for GD2 channel
- Strategy handler improvements

## v0.1 — Initial Beta Release

- Core MT5 bridge integration via CrossOver/Wine on macOS
- Telegram signal parsing for Gold Diggers VIP
- Five trading strategies
- Dynamic Position Management (DPM)
- Telegram bot and daily email reports
