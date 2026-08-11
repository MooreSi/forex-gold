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

## v0.7.1 — ML Engine Overhaul, Signal Bus & Silent Mac Launcher

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

## v0.6 — Remote Admin, Signal Generators & AI Analysis

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

## v0.5 — Bounce Generator & Real-Time Monitoring

- TEST tab renamed to Bounce Generator
- Signals section added to Trading > Strategy
- Bounce Generator live execution through full MT5 pipeline
- Real-time monitoring with 3-second velocity loop
- Outcome check interval reduced to 5 seconds
- Message detection latency reduced to ~18 ms

## v0.4 — Performance Improvements

- MT5 bridge persistent HTTP connection (~1.5ms round-trip)
- Adaptive monitor loop: 1-second polling when trades open
- Pre-fetch tick before order placement
- Telegram latency improvements (ConnectionTcpAbridged)
- TG Learning mode for signal scoring

## v0.3 — TEST Module (Claude Bounce Generator)

- TEST tab with isolated Claude-powered signal generator
- Virtual trading engine with 2% risk sizing
- Isolated database (test_signal.db)
- Post-trade learning notes and batch pattern analysis
- Circuit breaker after 3 consecutive losses

## v0.2 — Bug Fixes

- Fixed MT5 broker UTC+3 timestamp offset
- Signal parser edge cases for GD2 channel
- Strategy handler improvements

## v0.1 — Initial Beta Release

- Core MT5 bridge integration via CrossOver/Wine on macOS
- Telegram signal parsing for Gold Diggers VIP
- Five trading strategies
- Dynamic Position Management (DPM)
- Telegram bot and daily email reports
