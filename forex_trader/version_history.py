# Version history — single source of truth for the app version number.
# The first entry is always the current release.  __init__.py derives
# __version__ from it and writes forex_trader/VERSION so all other
# consumers (remote client, admin console, title banner) stay in sync.
#
# Each entry: (version_str, title, badge_colour, badge_label, [changes])

RELEASES: list[tuple] = [
    (
        "v0.8.2",
        "ORB Auto-Execute Fix, R:R Tracking & Version Reporting",
        "teal",
        "",
        [
            "Fixed ORB/IVB auto-execute firing an immediate market order at "
            "whatever price was live instead of waiting for price to retest "
            "the reported volume-profile reload zone — now places a pending "
            "order that only fills on a genuine zone retest (60-minute "
            "expiry, with a Telegram alert if the zone is never retested).",
            "ORB/IVB report now includes the Asian session range (high/low) "
            "and an untested-extension target beyond the primary breakout "
            "target; report naming corrected to consistently say 'ORB/IVB' "
            "instead of just 'IVB'.",
            "History > Trade History: added a R:R (reward:risk) column, "
            "computed per trade from its own entry/stop/TP1 levels.",
            "Fixed Max TP Hit not populating on trades opened by the other "
            "paired node — now falls back to the cross-node consolidated "
            "ledger, same as the Channel and Strategy columns already did.",
            "Reversal Engine: recalibrated signal-scoring weights against a "
            "backtest of 767 real Gold Diggers VIP/GD2 signals — round-number "
            "levels (round_5 especially) weighted up, Asia range/swing levels "
            "weighted down to match their measured real-world hit rates.",
            "Fixed Reversal Engine nightly research silently failing on high-message "
            "nights — AI call timeout widened from 60s to 240s, plus a "
            "dedicated failure-alert email so a failed run is never silent.",
            "Fixed app version reporting: Settings > Update 'Installed "
            "Version' and the admin console's per-client version display "
            "were reading a separately-maintained file that had drifted out "
            "of sync with the real release version — both now read the same "
            "canonical version source as the rest of the app.",
        ],
    ),
    (
        "v0.8.1",
        "Fix: missing dependencies",
        "orange",
        "",
        [
            "Fixed 'ModuleNotFoundError: cryptography' crash on startup — the "
            "package was imported by MT5 credential encryption and the sync "
            "channel's TLS certs but never declared in requirements.txt, so a "
            "fresh install/VPS never got it via pip install.",
            "Also declared python-dateutil explicitly (used directly in the news "
            "calendar) rather than relying on it arriving transitively through "
            "yfinance → pandas.",
        ],
    ),
    (
        "v0.8",
        "Local/Remote Sync",
        "blue",
        "",
        [
            "New Local/Remote mode toggle in the header (replaces the strategy badge) — a Mac and a VPS instance of this app can now pair up, with exactly one of them ever executing new trades at a time via a STAND_DOWN/RESUME handshake over a dedicated TLS-secured sync channel.",
            "Settings > Remote Node: configure this machine as the VPS (accept connections, generate a shared token) or as the client (connect to a VPS's fixed IP).",
            "Settings changes made on either node propagate to the other automatically — the VPS holds the authoritative copy.",
            "Consolidated trade ledger: closed trades from both nodes merge into one combined view without ever touching either side's own operational databases.",
            "Manual, on-demand ML model snapshot transfer between nodes (download/upload) — models otherwise train independently per node.",
            "Switching from Local back to Remote is blocked while this node still has open positions, rather than abandoning or silently orphaning them.",
        ],
    ),
    (
        "v0.7",
        "Windows Compatibility",
        "green",
        "",
        [
            "Native Windows support confirmed and hardened — watchdog, self-healing, automatic demo/live account switching, and automatic Algo Trading enable all verified working on native Windows exactly as on macOS, no CrossOver/Wine required.",
            "'Setup & Start FOREX.bat' now auto-launches the MetaTrader 5 terminal on startup if it isn't already running, mirroring the macOS '.command' script — reads the saved terminal path from bridge_credentials.json, waits for MT5 to initialise before the bridge attaches.",
            "Fixed two hardcoded macOS-only data paths (history.py trend report, signal_generator_report.py) that would have crashed on Windows — both now use the platform-aware DATA_DIR constant.",
            "Self-healer's bridge-restart-failure message is now platform-aware (\"check the MT5 terminal\" on Windows vs \"check Wine/CrossOver\" on macOS).",
            "Reversal Runner strategy added — new climber-ladder strategy wired through signal generation, pending-signal fill, tick dispatch, backtesting, and the Strategy dropdown.",
            "Setup Instructions page overhauled — added dedicated Windows setup steps (native MT5 install, running the .bat), corrected the Licence Key step to match the real activation flow (Machine ID detection, automated Request Registration, Manual Activation fallback), and clarified which steps are macOS-only vs Windows-only.",
            "Admin now receives a Telegram notification whenever a new user submits a registration / licence request, in addition to the existing admin console alert.",
        ],
    ),
    (
        "v0.6",
        "Channel Strategy Overrides & Circuit Breaker",
        "blue",
        "",
        [
            "Global circuit breaker — single DB-persisted CB replaces all per-engine streak halts. Configurable consecutive-loss threshold and cooldown; blocks only live MT5 execution while signals and TG parsing continue. Countdown banner in balance row. Telegram notification on block.",
            "Channel strategy overrides — per-channel strategy selection (Trailing Stop, Conservative, Scale Out, etc.) that correctly overrides the global Active Strategy for all execution paths including TG direct-fire and pending watcher.",
            "Channel strategy UI fix — dropdown now correctly saves on change (switched to on_value_change); was silently clearing overrides on every render due to NiceGUI event argument mismatch.",
            "Evaluate Now popup fix — switched to AsyncAnthropic client and direct on_click assignment; was blocking the event loop and losing NiceGUI client context.",
            "Breakout signal stuck-open fix — MT5 closure sync check added to both breakout and bounce engines; signals with a live trade closed between TP1 and final TP no longer get permanently stuck as triggered/open.",
            "Max TP Hit now evaluated against original Telegram signal TP levels, not the strategy's overridden TPs — accurately reflects how far price ran vs signal targets.",
            "Trailing Stop strategy — SL locks at entry on TP1, then trails price by configurable distance (pts); distance label corrected from $ to pts.",
        ],
    ),
    (
        "v0.5",
        "Bounce Generator & Real-Time Monitoring",
        "purple",
        "",
        [
            "TEST tab renamed to Bounce Generator — password gate removed, accessible directly from the main tab bar.",
            "Signals section added to Trading > Strategy — two independent toggles: Telegram Signals (accept/ignore TG channel signals) and Bounce Generator live execution (route generated signals through the full MT5 pipeline).",
            "Bounce Generator live execution — when enabled, triggered signals flow through the full main engine: Risk Governor, strategy SL/TP overrides, MT5 order placement, Telegram notification, History, and all stats. Virtual signal continues tracking independently.",
            "Real-time monitoring — velocity loop samples price every 3 seconds over a 20-second rolling window; movements exceeding 10 pts trigger an emergency scan bypassing the candle gate. Liquidity sweep detector watches M15 swing highs/lows for touch-then-reversal patterns.",
            "Outcome check interval reduced from 60 s to 5 s — TP/SL detection for virtual signals is now near-real-time.",
            "Message detection latency reduced from 480–1100 ms to ~18 ms — replaced sleep-poll loop with asyncio.Event wake-up, deferred sender resolution, and background DB writes.",
            "Recheck race window eliminated — open_trade_from_signal() now accepts a pre-fetched tick; when the signal generator supplies its verification tick the redundant bridge round-trip and zone-recheck window are skipped entirely.",
            "Conservative strategy fix — signal generator now correctly applies the active strategy's 5 pt fixed SL/TP from fill price (not ATR-based signal levels), and checks TP1 before TP3 in outcome tracking.",
            "TP ordering guard in live execution — TPs are sorted into valid direction order before passing to create_signal(), preventing validation errors when signal generator TPs are non-sequential.",
            "Risk Governor (Tier 1 safety layer) — configurable position sizing ceiling, daily-loss halt, and consecutive-loss streak halt; toggled in Risk Settings, off by default.",
        ],
    ),
    (
        "v0.4",
        "Performance Improvements",
        "teal",
        "",
        [
            "MT5 bridge persistent HTTP connection — single keep-alive TCP connection replaces a new handshake per request, cutting bridge round-trip from ~7ms to ~1.5ms.",
            "Adaptive monitor loop — tick and trade checking now runs every 1 second when trades are open (was 5 seconds), reducing TP/SL response lag by up to 4 seconds.",
            "Pre-fetch fresh tick before order placement — SL/TP calculations and fill-price fallback now use a live price fetched at the moment of order submission rather than a cached value.",
            "Telegram (Telethon) latency improvements — switched to ConnectionTcpAbridged (compact packet framing), moved network calls off the event handler onto a dedicated queue processor, reduced backup poll interval from 1s to 30s.",
            "DC alignment verified: Telethon session and both Gold Diggers channels confirmed on Telegram DC4 (Amsterdam) — optimal for UK location with no cross-datacenter relay.",
            "TG Learning mode — toggle on the TEST panel feeds recent Gold Diggers provider signals to Claude as context when scoring generated signals, improving directional alignment.",
            "Out-of-Hours improvements — OOH header badge now correctly shows the OOH strategy name (not the base strategy) when the time window is inactive; OOH strategy label reflected in open trade cards and Telegram messages.",
            "OOH holiday date range — calendar date picker added to Out-of-Hours settings with enable/disable checkbox, allowing multi-day holiday coverage beyond the time window.",
            "New Telegram commands: /restartapp (remote restart), /switchlive and /switchdemo (environment switching), /help updated.",
            "Weekend email suppression — daily summary emails no longer sent on Saturdays or Sundays.",
            "Bug fix: /restartapp restart loop — bot update offset now persisted to database and restored on startup; processed commands are never replayed across restarts.",
            "Bug fix: no-SL scale-out partial close executing at a loss — added breakeven guard to instant-fire TP1 path so partials only fire at market when price is at or better than entry.",
            "Bug fix: OOH banner showing wrong strategy name when OOH enabled but outside active window.",
        ],
    ),
    (
        "v0.3",
        "TEST Module — Claude Bounce Generator",
        "blue",
        "",
        [
            "Added TEST tab (password-protected, machine-locked) — isolated Claude-powered XAUUSD signal generator that runs entirely separately from the main app.",
            "Virtual trading engine: generates signals from rules-based analysis (H1 HTF bias, key levels, M15 candle confirmation) reviewed by Claude before entry.",
            "Virtual position sizing: 2% risk per trade, lot size auto-calculated from SL distance and running balance, starting at $1,000.",
            "Isolated database (test_signal.db) and dedicated log file — no data is shared with or affects the main application.",
            "Active positions and full trade history with dollar P&L, lot size, and running balance displayed after each closed trade.",
            "Post-trade learning notes: Claude explains why each trade won or lost in one sentence; batch pattern analysis every 10 trades.",
            "Performance analytics breakdown by session, HTF bias, and key level type to identify what conditions produce best results.",
            "Circuit breaker: pauses new signals after 3 consecutive losses; resets manually from the UI.",
            "Minimum quality score gate (60%) — Claude-scored signals below threshold are skipped.",
            "Fixed instant-entry signals executing with stop_loss=0.0 — provisional $60 protective stop now applied immediately and replaced when follow-up arrives.",
        ],
    ),
    (
        "v0.2",
        "Bug Fixes",
        "amber",
        "",
        [
            "Fixed MT5 broker timestamp offset — Vantage Markets server time is UTC+3; all deal history queries now apply the correct offset.",
            "Resolved signal parser edge cases for GD2 channel (GOLD DIGGERS 2.0) partial messages sent before SL/TP are appended.",
            "Strategy handler improvements across all five strategies — skipped TP levels now correctly inserted into vantage_partial_closes for UI chip display.",
            "Improved Telegram bot error messages for unknown strategy aliases.",
            "UI stability fixes: strategy description card no longer flickers on tab switch.",
        ],
    ),
    (
        "v0.1",
        "Initial Beta Release",
        "green",
        "Beta",
        [
            "Core MT5 bridge integration via CrossOver/Wine on macOS — supports demo and live accounts.",
            "Telegram signal parsing for Gold Diggers VIP (Format A/B) and GOLD DIGGERS 2.0 (Format C).",
            "Live tick feed with bid/ask/spread display and real-time P&L tracking.",
            "Five trading strategies: Scale Out + Breakeven, Breakeven Runner, Trailing Stop, Protected Scale, Conservative.",
            "Dynamic Position Management (DPM) with ATR-based trailing stop and peak P&L drawdown guard.",
            "Partial close engine with per-TP tracking in vantage_partial_closes table.",
            "Telegram bot (/strategy, /risk, /pause, /resume, /status, /close, /ime-on, /ime-off).",
            "Daily and weekly email reports via Resend or SMTP.",
            "AI trade commentary via Claude API (Anthropic).",
            "Strategy Builder for manual signal entry.",
            "History tab with closed trade review, MT5 import, and performance statistics.",
        ],
    ),
]
