"""
FOREX Trader — NiceGUI application entry point.
Initialises the engine and reader on startup, then defines the main page layout.
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

from nicegui import app, ui

# ── NiceGUI 3.12.x timer teardown patch ───────────────────────────────────────
# In NiceGUI 3.12.1, element.parent_slot raises RuntimeError (via dead weakref)
# when the page client disconnects and the slot is GC'd.
# elements/timer._get_context() calls parent_slot expecting None-or-slot, but
# gets an exception instead, producing log spam on every page navigation.
# Patch both methods so disconnect is silent.
from contextlib import nullcontext as _nullcontext
from nicegui.elements.timer import Timer as _UITimer
from nicegui.timer import Timer as _BaseTimer


def _safe_timer_get_context(self):
    try:
        return self.parent_slot or _nullcontext()
    except RuntimeError:
        self.cancel()
        return _nullcontext()


def _safe_timer_cleanup(self):
    _BaseTimer._cleanup(self)  # clears self.callback
    if not self._deleted:
        try:
            slot = self._parent_slot() if self._parent_slot else None
            if slot is not None:
                slot.parent.remove(self)
        except Exception:
            pass


_UITimer._get_context = _safe_timer_get_context
_UITimer._cleanup = _safe_timer_cleanup
# ── end patch ──────────────────────────────────────────────────────────────────

import forex_trader.config as cfg_module
from forex_trader import __version__ as _APP_VERSION
from forex_trader.core import database as db_module
from forex_trader.ui.pages import backtest as backtest_page

log = logging.getLogger(__name__)

# Admin panel availability + engine lifecycle now live in core/app_lifecycle.py
# so the headless entry point sees the same logic without importing NiceGUI.
from forex_trader.core.app_lifecycle import (          # noqa: E402
    admin_open_fn as _admin_open_fn, ADMIN_AVAILABLE,
    get_engine, get_tg_reader,
    startup as _lifecycle_startup, shutdown as _lifecycle_shutdown,
)

STATIC_DIR = Path(__file__).parent / "static"
app.add_static_files("/static", str(STATIC_DIR))

# Versioned favicon URL — Safari caches favicons in its own database keyed by
# URL, so a plain data URL or /favicon.ico won't bust its cache. Using the
# startup timestamp as a query string produces a URL Safari has never seen on
# each app restart, forcing it (and all other browsers) to fetch the new icon.
import time as _time
_FAVICON_VERSION = int(_time.time())
_FAVICON_HTML = (
    f'<link rel="icon" type="image/png" href="/static/favicon.png?v={_FAVICON_VERSION}">'
    if (STATIC_DIR / "favicon.png").exists() else ""
)

# ── Cash register sound — Web Audio API synthesis ────────────────────────────
# Played in the browser when a trade closes with a profit.
#
# IMPORTANT — browser autoplay policy:
#   AudioContext starts in 'suspended' state unless the user has interacted with
#   the page.  _VC_AUDIO_UNLOCK_JS must be injected once on page load; it attaches
#   click/keydown/touchstart listeners that create and resume the shared context.
#   _CASH_REGISTER_JS then reuses that already-running context.
#
_VC_AUDIO_UNLOCK_JS = """
<script>
(function() {
    window._vcAudioCtx = null;
    function _vcUnlock() {
        if (!window._vcAudioCtx) {
            try { window._vcAudioCtx = new (window.AudioContext || window.webkitAudioContext)(); }
            catch(e) { return; }
        }
        if (window._vcAudioCtx.state === 'suspended') {
            window._vcAudioCtx.resume().catch(function(){});
        }
    }
    ['click','keydown','touchstart'].forEach(function(ev) {
        document.addEventListener(ev, _vcUnlock, {passive: true});
    });
})();
</script>
"""

_CASH_REGISTER_JS = """
(function() {
    try {
        var ctx = window._vcAudioCtx;
        if (!ctx || ctx.state !== 'running') return;
        var t = ctx.currentTime;

        // Mechanical click — very short filtered noise burst
        var sr  = ctx.sampleRate;
        var len = Math.floor(sr * 0.025);
        var buf = ctx.createBuffer(1, len, sr);
        var d   = buf.getChannelData(0);
        for (var i = 0; i < len; i++) {
            d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / len, 2);
        }
        var click     = ctx.createBufferSource();
        var clickGain = ctx.createGain();
        click.buffer  = buf;
        clickGain.gain.setValueAtTime(0.5, t);
        click.connect(clickGain);
        clickGain.connect(ctx.destination);
        click.start(t);

        // Metallic "ching" ring — three harmonics, exponential decay
        [[659, 0.35, 0.7], [1319, 0.22, 0.45], [1976, 0.12, 0.3]].forEach(function(h) {
            var osc  = ctx.createOscillator();
            var gain = ctx.createGain();
            osc.type            = 'sine';
            osc.frequency.value = h[0];
            gain.gain.setValueAtTime(h[1], t + 0.008);
            gain.gain.exponentialRampToValueAtTime(0.0001, t + h[2]);
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.start(t + 0.008);
            osc.stop(t + h[2]);
        });
    } catch(e) {}
})();
"""

# ── App-wide singletons ────────────────────────────────────────────────────────
# Engine/bridge/Telegram/sync lifecycle lives in core/app_lifecycle.py so the
# headless entry point (run_headless.py) can call the exact same startup()/
# shutdown() without importing NiceGUI. These just wire it to NiceGUI's hooks.

@app.on_startup
async def startup():
    await _lifecycle_startup()


@app.on_shutdown
async def shutdown():
    await _lifecycle_shutdown()


# ── Main page ──────────────────────────────────────────────────────────────────

def _render_about():
    _sub: list[str] = ["home"]   # mutable so nested functions can mutate

    # ── Sub-page content containers ───────────────────────────────────────────
    content_area = ui.column().classes("w-full")

    def _show_home():
        _sub[0] = "home"
        content_area.clear()
        with content_area:
            with ui.column().classes("w-full max-w-3xl gap-6 p-6"):
                with ui.row().classes("items-center gap-3"):
                    ui.label("FOREX Trader").classes("text-2xl font-bold text-yellow-400")
                    ui.label("by Simon Moore").classes("text-lg").style("color:#38bdf8")
                    ui.badge(f"BETA v{_APP_VERSION}", color="green").classes("text-xs")
                ui.separator()

                # Risk disclaimer
                with ui.card().classes("w-full rounded-lg p-4").style(
                    "background:#1a1008;border:1px solid #92400e;"
                ):
                    with ui.row().classes("items-start gap-3"):
                        ui.label("warning").classes(
                            "material-icons text-orange-400 text-xl shrink-0 mt-0.5"
                        )
                        with ui.column().classes("gap-2"):
                            ui.label("Risk Warning").classes(
                                "text-xs font-bold text-orange-400 uppercase tracking-wider"
                            )
                            ui.label(
                                "Trading leveraged financial instruments such as gold (XAUUSD) "
                                "carries a high level of risk and may not be suitable for all "
                                "investors. A significant proportion of retail trader accounts "
                                "lose money when trading CFDs and similar products. You should "
                                "not risk capital you cannot afford to lose."
                            ).classes("text-xs text-orange-200 leading-relaxed")
                            ui.label(
                                "This software is provided as-is, without warranty of any kind. "
                                "It may contain bugs or produce incorrect results. Automated "
                                "execution does not guarantee profitability and can result in "
                                "losses that exceed your initial deposit. You are solely "
                                "responsible for monitoring all open positions and for any "
                                "trading decisions made, whether manually or via automation. "
                                "Do not leave automated trading running unattended for extended "
                                "periods without reviewing open positions."
                            ).classes("text-xs text-orange-200 leading-relaxed")
                            ui.label(
                                "Past performance is not indicative of future results. "
                                "This tool does not constitute financial advice."
                            ).classes("text-xs text-orange-300 font-semibold leading-relaxed")

                # Nav cards
                ui.label("Sections").classes("text-sm font-semibold text-gray-400 uppercase tracking-wider")
                with ui.row().classes("gap-4 flex-wrap w-full items-stretch"):
                    for icon, label, desc, action in [
                        ("smart_toy",   "Bot Orchestration",
                         "Detailed guide to signal processing, strategies, DPM, and all automation features.",
                         lambda: _show_section("orchestration")),
                        ("menu_book",   "Setup Instructions",
                         "Step-by-step setup for CrossOver, MT5, Telegram, email and going live.",
                         lambda: _show_section("instructions")),
                        ("history",     "Version History",
                         "Release notes and changelog for each version of FOREX Trader.",
                         lambda: _show_section("version")),
                    ]:
                        with ui.card().classes(
                            "flex-1 min-w-56 bg-gray-800 rounded-lg p-4 cursor-pointer "
                            "hover:bg-gray-700 transition-colors self-stretch"
                        ).on("click", action):
                            with ui.row().classes("items-center gap-2 mb-2"):
                                ui.label(icon).classes(
                                    "material-icons text-yellow-400 text-xl"
                                )
                                ui.label(label).classes(
                                    "text-sm font-semibold text-gray-100"
                                )
                            ui.label(desc).classes("text-xs text-gray-400 leading-relaxed")
                            ui.label("Open →").classes("text-xs text-yellow-500 mt-2")

    def _sub_header(title: str, icon: str):
        with ui.row().classes("w-full items-center gap-3 mb-4"):
            ui.button(icon="arrow_back", on_click=_show_home).classes(
                "bg-gray-700 text-white text-xs px-3 py-1"
            ).tooltip("Back to About")
            ui.label(icon).classes("material-icons text-yellow-400 text-xl")
            ui.label(title).classes("text-xl font-bold text-yellow-300")

    def _show_section(section: str):
        _sub[0] = section
        content_area.clear()
        with content_area:
            with ui.column().classes("w-full max-w-5xl gap-4 p-6"):
                if section == "orchestration":
                    _sub_header("Bot Orchestration", "smart_toy")
                    orch_items = [
                        ("Signal Sources", "smart_toy",
                         "Two independent Telegram listener slots monitor channels in real-time. "
                         "Each slot subscribes to a channel of your choice. Signals are parsed, "
                         "validated for XAUUSD content, and queued automatically. Duplicate and "
                         "out-of-hours signals are filtered before they reach the execution layer."),
                        ("Auto-Execution", "flash_on",
                         "Enable Auto-Execute in Trading > Strategy to have qualifying signals "
                         "open MT5 positions automatically. Before a trade opens, the bot checks: "
                         "signal is within the entry range, risk limits are not exceeded, the "
                         "bridge is connected, and trading is not paused. Signals that fail any "
                         "check are flagged as 'pending' and can be manually activated."),
                        ("Dynamic Position Management (DPM)", "tune",
                         "DPM replaces the standard fixed-TP strategy with intelligent live "
                         "position management. Once a trade is open, DPM monitors price "
                         "continuously, trails the stop-loss as price moves in your favour, "
                         "locks in partial profits at key levels, and closes the remaining "
                         "position if price reverses beyond a configurable drawdown threshold. "
                         "DPM uses peak P&L tracking to avoid giving back too much open profit. "
                         "Enable it in Trading > Strategy. When DPM is active, the "
                         "standard Scale Out / BE Runner strategies are suspended for that trade."),
                        ("Profit Close Target", "flag",
                         "Set a cumulative USD profit target in Trading > Strategy > Risk Settings. "
                         "The bot tracks total realised profit from partial closes plus current "
                         "unrealised P&L. The moment the combined figure reaches your target "
                         "the remaining position is closed. Set to $0 to disable. "
                         "Example: target $50 — bot closes $10 at TP1, then when unrealised "
                         "P&L on the remaining position reaches $40, the trade closes for "
                         "a total of $50."),
                        ("Strategy Engine", "account_tree",
                         "Four fixed strategies are available when DPM is off. Scale Out closes "
                         "a portion at each TP and moves SL to breakeven after TP1. BE Runner "
                         "keeps the full position open and advances SL to each cleared TP level. "
                         "Trailing Stop moves SL a fixed number of pips behind price as it "
                         "advances. Protected Scale closes portions at TPs and uses a tighter "
                         "trailing stop. Switch strategy in Trading > Strategy or via "
                         "/strategy in the Telegram bot."),
                        ("Out-of-Hours Filtering", "schedule",
                         "Signals received outside your configured trading hours are not "
                         "auto-executed. They are stored as 'pending' and can be reviewed in "
                         "the TG Signals table. Out-of-hours filtering prevents overnight or "
                         "low-liquidity executions without oversight. Active trade management "
                         "(SL/TP monitoring) continues as normal even when auto-execution is "
                         "paused."),
                        ("Immediate Market Entry (IME)", "bolt",
                         "When IME is enabled a signal is executed immediately at the current "
                         "market price rather than waiting for price to enter the entry range. "
                         "Useful for fast-moving markets where waiting for a precise entry would "
                         "miss the move. Enable via /ime-on in the Telegram bot or in "
                         "Trading > Strategy. IME fires once per signal."),
                        ("Strategy Builder", "construction",
                         "The Strategy Builder in Trading > Build Signal lets you construct a "
                         "custom signal manually: set direction, entry range, stop-loss, and "
                         "up to 8 TP levels. The built signal enters the same execution pipeline "
                         "as a Telegram signal and respects all risk and DPM settings. Use it "
                         "to place trades based on your own analysis without needing a "
                         "Telegram signal."),
                        ("Pause Trading", "pause_circle",
                         "Use the pause button in the top bar to halt all new trade execution "
                         "for a set number of hours or until a specific date and time. Existing "
                         "open trades continue to be managed normally. Resume at any time with "
                         "/resume in the Telegram bot or by clicking Resume in the pause dialog."),
                    ]
                    for title, icon_name, body in orch_items:
                        with ui.card().classes("w-full bg-gray-800 rounded-lg p-4"):
                            with ui.row().classes("items-center gap-2 mb-2"):
                                ui.label(icon_name).classes("material-icons text-yellow-400 text-base")
                                ui.label(title).classes("text-sm font-semibold text-yellow-300")
                            ui.label(body).classes("text-xs text-gray-300 leading-relaxed")

                elif section == "registration":
                    _sub_header("Registration & Setup", "how_to_reg")
                    steps = [
                        ("1. Vantage Markets Account", "Open a demo or live account at vantagemarkets.com. Download MetaTrader 5 and log in to your account."),
                        ("2. MT5 Bridge", "The local Python bridge (mt5_bridge.py) must be running — it connects MT5 to this app via localhost:9000. Start it via FOREX Start.command or the power button."),
                        ("3. Anthropic API Key", "Go to console.anthropic.com, create an API key and paste it in Settings > Bridge & Config. Required for AI analysis and trade commentary."),
                        ("4. Telegram Setup", "Go to my.telegram.org/apps and create an application to get your API ID and API Hash. Enter them in Settings > Telegram, then authenticate on the Telegram tab."),
                        ("5. Email Reports", "Go to Settings > Email Reports. Use Mailjet (recommended — free, no SMTP issues) or configure Gmail SMTP with an App Password."),
                        ("6. Live Trading", "When ready for live trading, switch to Live in the Trading tab toggle. Change your MT5 credentials to VantageMarkets-Live in Settings first."),
                    ]
                    for title, body in steps:
                        with ui.card().classes("w-full bg-gray-800 rounded-lg p-4"):
                            ui.label(title).classes("text-sm font-semibold text-yellow-300 mb-1")
                            ui.label(body).classes("text-xs text-gray-300 leading-relaxed")

                elif section == "instructions":
                    _sub_header("Setup Instructions", "menu_book")

                    def _render_setup_cards(sections):
                        for title, icon_name, steps in sections:
                            with ui.card().classes("w-full bg-gray-800 rounded-lg p-5"):
                                with ui.row().classes("items-center gap-2 mb-3"):
                                    ui.label(icon_name).classes("material-icons text-yellow-400 text-lg")
                                    ui.label(title).classes("text-base font-semibold text-yellow-300")
                                with ui.column().classes("w-full gap-1.5 pl-1"):
                                    for i, step in enumerate(steps, 1):
                                        with ui.row().classes("items-start gap-3"):
                                            ui.label(f"{i}.").classes(
                                                "text-xs text-gray-500 font-mono shrink-0 mt-0.5 w-4 text-right"
                                            )
                                            ui.label(step).classes("text-sm text-gray-300 leading-relaxed flex-1")

                    windows_sections = [
                        (
                            "1. Windows Setup — MetaTrader 5",
                            "desktop_windows",
                            [
                                "No compatibility layer is needed on Windows — MT5 and the app both run natively.",
                                "Download MetaTrader 5 directly from the Vantage Markets website (or metatrader5.com) and run the installer.",
                                "Open MetaTrader 5 and log in with your Vantage Markets demo or live credentials (server: VantageMarkets-Demo or VantageMarkets-Live).",
                                "Keep MetaTrader 5 open and running whenever you use this app. FOREX Trader connects to it directly, in-process — there is no separate bridge program to install or start on Windows.",
                                "Enable Algo Trading in the MT5 toolbar: click the robot icon so it turns green. This must be ON for the app to place any trades. MT5 turns it off automatically after a terminal restart or account switch, so re-enable it each time you reopen MT5.",
                                "Once a terminal path is saved in Settings > MT5 / Bridge, 'Setup & Start FOREX.bat' will automatically launch MT5 for you on startup if it isn't already running.",
                            ],
                        ),
                        (
                            "2. Windows Setup — Running the App",
                            "play_circle",
                            [
                                "Double-click 'Setup & Start FOREX.bat' in the project folder to install and launch the app — no CrossOver, Wine, or manual Python setup required.",
                                "On first run it locates or auto-installs Python 3.11+, creates a virtual environment, and installs all dependencies. This takes about a minute and only repeats when dependencies change.",
                                "The app then opens automatically in your browser at http://localhost:8888.",
                                "Leave the command window open while the app is running — closing it stops the app. To stop manually, close the window or press Ctrl+C.",
                            ],
                        ),
                    ]

                    mac_sections = [
                        (
                            "1. macOS Setup — CrossOver",
                            "window",
                            [
                                "CrossOver is required to run MetaTrader 5 (a Windows application) on macOS — MT5 has no native Mac version and Apple Silicon Macs can't run the MetaTrader5 Python package directly.",
                                "Go to codeweavers.com and purchase or trial CrossOver.",
                                "Download and install the CrossOver .dmg file. Open CrossOver from your Applications folder.",
                                "CrossOver creates a 'bottle' (a Windows compatibility layer). MetaTrader 5 will be installed into this bottle.",
                            ],
                        ),
                        (
                            "2. macOS Setup — MetaTrader 5 in CrossOver",
                            "computer",
                            [
                                "In CrossOver, click 'Install a Windows Application'.",
                                "Search for 'MetaTrader 5' — CrossOver has a built-in installer for it.",
                                "Follow the installer and let it create a bottle named 'MetaTrader 5'.",
                                "Once installed, open MetaTrader 5 from within CrossOver.",
                                "Log in with your Vantage Markets demo or live credentials (server: VantageMarkets-Demo or VantageMarkets-Live).",
                                "Keep MetaTrader 5 open and running whenever you use this app — the local bridge (next section) requires it.",
                                "Enable Algo Trading in the MT5 toolbar: click the robot icon so it turns green. This must be ON for the app to place any trades. MT5 turns it off automatically after a terminal restart or account switch, so re-enable it each time you reopen MT5.",
                            ],
                        ),
                        (
                            "3. macOS Setup — MT5 Bridge",
                            "cable",
                            [
                                "The bridge (mt5_bridge.py) is a small Python server that runs inside the CrossOver bottle and translates calls from this app into MT5 actions — this extra step only exists on macOS, because MT5 runs under Wine there and the app's own Python can't import the MetaTrader5 package directly. Windows doesn't need this at all — the app talks to MT5 in-process natively.",
                                "Start it by clicking the power button in the top bar and selecting 'Start Bridge', or double-click 'FOREX Start.command' in the project folder.",
                                "The bridge must be running at all times when using trading features. It listens on http://localhost:9000.",
                                "If the bridge stops, the app's built-in watchdog and self-healer will usually restart it automatically. If it doesn't, restart the app. The app shows live bridge status in Settings > MT5 / Bridge.",
                            ],
                        ),
                    ]

                    vps_sections = [
                        (
                            "1. VPS Setup — Windows & MT5",
                            "dns",
                            [
                                "A VPS running FOREX Trader is just a headless Windows machine — MT5 and the app run natively, the same as a native Windows install. Connect via Remote Desktop (RDP) to do the setup below.",
                                "Download MetaTrader 5 from the Vantage Markets website and log in with your demo or live account credentials.",
                                "Enable Algo Trading (robot icon → green) in the MT5 toolbar — required for any trade to be placed. Re-enable it after every MT5 restart or account switch.",
                                "FOREX Trader connects to MT5 directly in-process on Windows — there is no separate bridge program to install or run.",
                            ],
                        ),
                        (
                            "2. VPS Setup — Firewall rules",
                            "security",
                            [
                                "Open Windows Defender Firewall with Advanced Security on the VPS (and your hosting provider's network security group / cloud firewall in front of it, if it has one separate from Windows itself).",
                                "Allow inbound Remote Desktop (TCP 3389) so you can manage the VPS at all — most providers enable this by default. Restrict the allowed source IP to your own where possible.",
                                "Allow inbound TCP 8765 — this is the Sync Server port the paired Mac (or other local machine) connects to for Local/Remote pairing (Settings > Remote Node). It's the only FOREX Trader–specific port the VPS needs to accept inbound; the connection is TLS-encrypted and token-authenticated.",
                                "Do not open port 8888 to the internet — that's the app's own dashboard and it has no separate login of its own. Access it only via RDP, then browse to http://localhost:8888 inside the remote desktop session.",
                                "Ports 9000 and 9101 never need a firewall rule at all: 9000 was the old HTTP MT5 bridge and isn't used on native Windows, and 9101 (the EA bridge, if you use the MQL5 Expert Advisor handoff) only ever talks to itself on localhost.",
                            ],
                        ),
                        (
                            "3. VPS Setup — Running headless",
                            "cloud",
                            [
                                "A VPS is meant to run unattended. Before disconnecting your RDP session, enable Headless mode in Settings > Remote Node — this stops the app trying to open a browser window that has nowhere to display once you disconnect.",
                                "Use Windows Task Scheduler to auto-start the app at boot and keep it running: create a task that launches 'Setup & Start FOREX.bat' at system startup, set it to run whether a user is logged on or not, and enable 'Restart the task if it fails' in the task's Settings tab so it recovers automatically after a crash or VPS reboot.",
                                "Keep MetaTrader 5 itself set to auto-login on startup in its own options, so both MT5 and FOREX Trader come back up together after a reboot without needing to RDP in immediately.",
                            ],
                        ),
                        (
                            "4. VPS Setup — Pairing with your Mac or PC",
                            "sync",
                            [
                                "On the VPS, go to Settings > Remote Node, enable 'This machine is the VPS', and note the fingerprint and shared token shown.",
                                "On your Mac or other local machine, go to Settings > Remote Node > 'Connect to a remote VPS', enter the VPS's IP or hostname and the shared token, then click Save & Connect.",
                                "Use the Local/Remote toggle in the top header bar to choose which node actively places trades. Only one node should be the active trader at a time — both sides still receive every Telegram signal independently, but the one that isn't active stands down from placing real orders, so you never get duplicate trades on the same MT5 account.",
                                "Signal generator panels (Bounce, Breakout, GD Copy) mirror whichever node is active: when the VPS is active, the Mac's own panels show the VPS's real balance, stats and Circuit Breaker status, and Start/Stop/Run Now/Reset buttons act on the VPS instead of the Mac's stood-down local copy.",
                            ],
                        ),
                    ]

                    common_sections = [
                        (
                            "Expert Advisor (EA Bridge) — optional",
                            "hub",
                            [
                                "Optional and off by default. When off, Python manages every trade tick-by-tick exactly as it always has — nothing below is required. When on, a companion MQL5 Expert Advisor takes over placing and managing eligible strategies' orders directly inside MT5's own tick loop, which reacts faster than Python's ~1-second polling cycle. DPM always stays Python-managed regardless, since it needs live calibration data only the app holds.",
                                "In MT5, go to File > Open Data Folder, then open the MQL5 > Experts folder. Copy 'ForexTraderBridge.mq5' from the project's mql5 folder into it.",
                                "In MT5, open MetaEditor (F4), find ForexTraderBridge.mq5 in its Navigator, and press Compile (F7). This creates ForexTraderBridge.ex5 in the same folder — a one-time step per terminal, repeated only if the .mq5 source is ever updated.",
                                "Back in MT5, right-click Expert Advisors in the Navigator and choose Refresh so 'ForexTraderBridge' appears, then drag it onto the XAUUSD chart.",
                                "In the dialog that opens, go to the Common tab and tick 'Allow Algo Trading' for this EA specifically — separate from (and in addition to) the terminal-wide Algo Trading toggle in the main toolbar.",
                                "One-time terminal setting, required or the EA can never connect: go to Tools > Options > Expert Advisors, tick 'Allow WebRequest/Socket for listed addresses', and add 127.0.0.1 to the list.",
                                "In the app, go to Settings > MT5 / Bridge, enable the 'EA Bridge (experimental)' switch, and click Save. The app only hands a trade to the EA when it detects a live, connected EA on that terminal — if the EA is disconnected or was never attached, every trade is managed by Python instead, with no other change needed.",
                                "To confirm it's working: with the EA attached and the switch on, open a small test trade. You should see the EA's own activity in MT5's Experts tab (bottom panel) alongside the position.",
                            ],
                        ),
                        (
                            "Telegram Bot (trade alerts)",
                            "send",
                            [
                                "The bot sends you notifications when trades open, hit a TP, or close.",
                                "Open Telegram and search for @BotFather. Send /newbot and follow the prompts to create a bot.",
                                "BotFather will give you a token in the format 1234567890:ABCdef... — copy this.",
                                "To find your Chat ID: send a message to your new bot, then message @userinfobot or @RawDataBot and it will return your chat ID.",
                                "Paste the bot token and chat ID into Settings > Telegram Alerts, enable the checkbox, and save.",
                            ],
                        ),
                        (
                            "Telegram Reader (signal channels)",
                            "mark_chat_read",
                            [
                                "The reader monitors Telegram channels for trading signals in real-time. It requires your personal Telegram API credentials — not a bot token.",
                                "Go to my.telegram.org/apps and sign in with your Telegram account phone number.",
                                "Click 'Create new application'. Fill in any app name and short name (e.g. 'FOREX Reader'). The platform can be set to 'Desktop'.",
                                "You will receive an API ID (a number) and an API Hash (a long string). Copy both.",
                                "Enter them in Settings > Telegram Reader along with your phone number (international format: +441234567890), then click Save.",
                                "On the Telegram tab, click Authenticate and enter the OTP code sent to your Telegram account.",
                                "Once authenticated, click Load Groups to see available channels, then select a channel for Slot 1 and optionally Slot 2.",
                            ],
                        ),
                        (
                            "Resend (email reports)",
                            "email",
                            [
                                "Resend is the recommended email provider — it works over HTTPS with no SMTP setup, app passwords, or firewall issues.",
                                "Go to resend.com and sign up for free (3,000 emails/month, no credit card required).",
                                "In the Resend dashboard, go to API Keys and click Create API Key. Give it any name.",
                                "Copy the key (it starts with re_...) and paste it into Settings > Email Reports > Resend API Key.",
                                "Set your own email address in the 'Send reports to' field in the SMTP section below Resend.",
                                "Enable Daily or Weekly summary in the Reports section, set your preferred send time, and save.",
                                "Click 'Send Test Email' in the Resend section to confirm delivery before enabling scheduled reports.",
                            ],
                        ),
                        (
                            "Anthropic API Key (AI features)",
                            "smart_toy",
                            [
                                "An Anthropic API key enables Claude AI commentary on trades and the AI Analysis tab.",
                                "Go to console.anthropic.com and sign in or create an account.",
                                "Navigate to API Keys and click Create Key. Give it a name such as 'FOREX Trader'.",
                                "Copy the key and paste it into Settings > Bridge & Config > Anthropic API Key, then click Save Config.",
                                "Claude Sonnet is the default model — a good balance of speed and quality. Switch to Haiku for lower cost or Opus for deeper analysis.",
                            ],
                        ),
                        (
                            "Licence Key",
                            "vpn_key",
                            [
                                "A licence is required to use the app — if one isn't activated yet, the app shows an activation screen on launch instead of starting normally.",
                                "Your Machine ID is detected automatically and shown on that screen. Your administrator uses it to generate your licence.",
                                "Enter your name/nickname and email, then click 'Request Registration'. This sends an automated request to the licence server and the app activates itself once your administrator approves it — no need to do anything else.",
                                "If the licence server is unreachable, open 'Manual Activation', paste the licence key your administrator sent you directly (format KEY|EXPIRY_DATE), and click 'Activate Manually'.",
                                "The licence is tied to your Machine ID. If you reinstall or move to a new machine, contact your administrator to issue a new key.",
                                "Once activated, your registration details (email, masked key, machine ID, licence type, days remaining) are visible read-only in Settings > Registration.",
                            ],
                        ),
                        (
                            "Going Live",
                            "trending_up",
                            [
                                "Before switching to live trading, ensure all credentials, risk settings and strategies have been tested in demo mode.",
                                "In Settings > MT5 Credentials, fill in your Live login, password, and server (VantageMarkets-Live).",
                                "In the Trading tab, use the toggle at the top to switch from Simulation to Live. You will be prompted to confirm.",
                                "Start conservatively — use 0.5% or less risk per trade and a max lot size limit until you are confident in the setup.",
                                "Monitor the app closely during the first live session. The History tab shows all closed trades and account equity.",
                            ],
                        ),
                    ]

                    with ui.tabs().classes("w-full bg-gray-800 rounded-lg") as platform_tabs:
                        t_win_setup = ui.tab("Windows", icon="desktop_windows")
                        t_mac_setup = ui.tab("Mac", icon="laptop_mac")
                        t_vps_setup = ui.tab("VPS", icon="dns")

                    with ui.tab_panels(platform_tabs, value=t_win_setup).classes(
                        "w-full bg-transparent"
                    ).style("padding:0"):
                        with ui.tab_panel(t_win_setup).classes("w-full gap-4").style("padding:0"):
                            with ui.column().classes("w-full gap-4"):
                                _render_setup_cards(windows_sections)
                        with ui.tab_panel(t_mac_setup).classes("w-full gap-4").style("padding:0"):
                            with ui.column().classes("w-full gap-4"):
                                _render_setup_cards(mac_sections)
                        with ui.tab_panel(t_vps_setup).classes("w-full gap-4").style("padding:0"):
                            with ui.column().classes("w-full gap-4"):
                                _render_setup_cards(vps_sections)

                    ui.separator().classes("my-2")
                    ui.label("Common Setup — same on every platform").classes(
                        "text-sm font-semibold text-gray-400 uppercase tracking-wider"
                    )
                    _render_setup_cards(common_sections)

                elif section == "version":
                    _sub_header("Version History", "history")

                    from forex_trader.version_history import RELEASES as releases

                    for ver, title, badge_colour, badge_label, changes in releases:
                        # Auto-mark whichever entry matches the running version as Current
                        is_current = ver == f"v{_APP_VERSION}"
                        with ui.card().classes("w-full bg-gray-800 rounded-lg p-5"):
                            with ui.row().classes("items-center gap-3 mb-3"):
                                ui.label(ver).classes(
                                    "text-lg font-bold font-mono text-yellow-300"
                                )
                                ui.label(title).classes("text-base font-semibold text-gray-100")
                                if is_current:
                                    ui.badge("Current", color="green").classes("text-xs ml-1")
                                elif badge_label:
                                    ui.badge(badge_label, color=badge_colour).classes("text-xs ml-1")
                            with ui.column().classes("w-full gap-1.5 pl-1"):
                                for change in changes:
                                    with ui.row().classes("items-start gap-2"):
                                        ui.label("chevron_right").classes(
                                            "material-icons text-yellow-500 text-sm shrink-0 mt-0.5"
                                        )
                                        ui.label(change).classes(
                                            "text-sm text-gray-300 leading-relaxed flex-1"
                                        )


    _show_home()


@ui.page("/")
def main_page():
    import os
    import subprocess
    import sys
    import time as _time
    from forex_trader.ui.pages import trading, telegram, history, settings as settings_page
    import forex_trader.ui.pages.chart as chart_page
    import forex_trader.ui.pages.ai_summary as ai_summary_page
    import forex_trader.ui.pages.test_panel as test_panel
    import forex_trader.ui.pages.edge_dashboard as edge_dashboard

    ui.query("body").style(
        "background: #0f1117; color: #e0e0e0; font-family: 'Inter', sans-serif; margin:0;"
    )

    ui.add_head_html(
        '<script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.3/dist/confetti.browser.min.js"></script>'
    )
    if _FAVICON_HTML:
        ui.add_head_html(_FAVICON_HTML)
    # Unlock Web Audio API on first user interaction so the cash-register sound
    # can play from timer callbacks (which are not user gestures).
    ui.add_head_html(_VC_AUDIO_UNLOCK_JS)

    root = Path(__file__).parent.parent.parent

    # ── Power dialog (defined BEFORE header so it renders at root level) ────────
    with ui.dialog() as _power_dialog, ui.card().classes(
        "bg-gray-800 p-5 rounded-lg min-w-72"
    ):
        ui.label("Power Options").classes("text-base font-semibold text-white mb-1")
        ui.label(
            "Restart relaunches the app. Your browser reconnects automatically in ~5 s."
        ).classes("text-xs text-gray-400 mb-4")

        async def _do_restart():
            _power_dialog.close()
            ui.notify("Restarting — browser will reconnect in ~5 seconds...", type="info")
            # Use the venv Python directly — sys.executable resolves symlinks to
            # the system Python which has none of the venv packages installed.
            if sys.platform == "win32":
                venv_python = root / ".venv" / "Scripts" / "python.exe"
            else:
                venv_python = root / ".venv" / "bin" / "python3"
            python = str(venv_python) if venv_python.exists() else sys.executable
            from forex_trader.core.platform_utils import delayed_relaunch_cmd, open_restart_log
            from forex_trader.config import USER_DATA_DIR
            log_path = USER_DATA_DIR / "data" / "restart.log"
            with open_restart_log(log_path) as _restart_log:
                subprocess.Popen(
                    delayed_relaunch_cmd(python, "run.py", delay_secs=5, extra_args=["--no-browser"]),
                    cwd=str(root),
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=_restart_log,
                    stderr=_restart_log,
                )
            await asyncio.sleep(1)
            app.shutdown()

        async def _do_stop():
            _power_dialog.close()
            ui.notify("Shutting down FOREX Trader...", type="warning")
            await asyncio.sleep(1)
            app.shutdown()

        with ui.row().classes("gap-2"):
            ui.button("Restart", icon="refresh", on_click=_do_restart).classes(
                "bg-blue-700 text-white px-4 py-2"
            )
            ui.button("Stop", icon="power_off", on_click=_do_stop).classes(
                "bg-red-700 text-white px-4 py-2"
            )
            ui.button("Cancel", on_click=_power_dialog.close).classes(
                "bg-gray-700 text-white px-4 py-2"
            )

    # ── Pause dialog (defined BEFORE header) ─────────────────────────────────
    with ui.dialog() as _pause_dialog, ui.card().classes(
        "bg-gray-800 p-5 rounded-lg min-w-80"
    ):
        ui.label("Pause Trading").classes("text-base font-semibold text-yellow-300 mb-1")
        ui.label(
            "While paused, all signal generators and Telegram signals continue to run normally "
            "but no orders will be sent to MT5. "
            "Active trade management (SL/TP monitoring) continues as normal."
        ).classes("text-xs text-gray-400 mb-4")

        # Status label — refreshed each time the dialog opens
        _pause_status_lbl = ui.label("").classes(
            "text-yellow-400 text-sm font-semibold mb-3"
        ).style("display:none")

        ui.label("Pause for:").classes("text-sm text-gray-300 mb-1")
        pause_hours = ui.number("Hours", value=4, min=0.25, max=168, step=0.25, format="%.2f").classes("w-full")
        pause_hours.tooltip("Number of hours to pause trading. 0.25 = 15 minutes.")

        ui.label("Or pause until a specific time (local time):").classes("text-sm text-gray-300 mt-3 mb-1")
        pause_until_inp = ui.input(
            "Date & time (YYYY-MM-DD HH:MM)",
            value="",
        ).classes("w-full")
        pause_until_inp.tooltip(
            "Enter a specific date and time to pause until. "
            "Leave blank to use the hours field above."
        )

        pause_result = ui.label("").classes("text-xs text-gray-400 mt-2")

        async def _do_pause():
            from datetime import datetime as _dt
            try:
                if pause_until_inp.value.strip():
                    # User types local time; strptime gives a naive local datetime
                    dt_local = _dt.strptime(pause_until_inp.value.strip(), "%Y-%m-%d %H:%M")
                    pause_ts = dt_local.timestamp()
                else:
                    pause_ts = _time.time() + float(pause_hours.value or 4) * 3600

                if pause_ts <= _time.time():
                    pause_result.text = "Pause time must be in the future"
                    return

                db_module.set_app_config("trade_pause_until", str(pause_ts))
                exp = _dt.fromtimestamp(pause_ts)
                _pause_dialog.close()
                ui.notify(
                    f"Trading paused until {exp.strftime('%d %b %H:%M')}",
                    type="warning",
                )
            except Exception as e:
                pause_result.text = f"Error: {e}"

        def _do_resume():
            db_module.set_app_config("trade_pause_until", "0")
            _pause_dialog.close()
            ui.notify("Trading resumed", type="positive")

        with ui.row().classes("gap-2 mt-3"):
            ui.button(
                "Pause Now", icon="pause",
                on_click=_do_pause,
            ).classes("bg-yellow-700 text-white px-4 py-2")
            _resume_btn = ui.button(
                "Resume Trading", icon="play_arrow",
                on_click=_do_resume,
            ).classes("bg-green-700 text-white px-4 py-2").style("display:none")
            ui.button("Cancel", on_click=_pause_dialog.close).classes(
                "bg-gray-700 text-white px-4 py-2"
            )

        def _on_pause_dialog_change(e):
            """Refresh status label and Resume button whenever the dialog opens."""
            if not e.value:
                return
            from datetime import datetime as _dt
            raw = db_module.get_app_config("trade_pause_until")
            paused = raw is not None and float(raw or 0) > _time.time()
            if paused:
                try:
                    exp = _dt.fromtimestamp(float(raw))
                    _pause_status_lbl.text = f"Currently PAUSED until {exp.strftime('%d %b %Y %H:%M')}"
                except Exception:
                    _pause_status_lbl.text = "Currently PAUSED"
                _pause_status_lbl.style("display:block")
                _resume_btn.style("display:inline-flex")
            else:
                _pause_status_lbl.style("display:none")
                _resume_btn.style("display:none")

        _pause_dialog.on_value_change(_on_pause_dialog_change)

    # ── Compact ticker strip ───────────────────────────────────────────────────
    _prev_bid: list[Optional[float]] = [None]
    _price_hist: list[float]         = []

    with ui.row().classes(
        "w-full items-center bg-gray-900 border-b border-gray-700 px-3 gap-0"
    ).style("height:54px; min-height:54px;"):

        # Banner / logo
        banner = STATIC_DIR / "banner.png"
        if banner.exists():
            ui.image("/static/banner.png").classes("h-10 object-contain pr-3 shrink-0")
        else:
            with ui.column().classes("pr-3 gap-0 justify-center shrink-0"):
                ui.label("FOREX Trader").classes(
                    "text-sm font-bold text-yellow-400 leading-none"
                )
                ui.label("by Simon Moore").classes(
                    "text-xs leading-none"
                ).style("color:#38bdf8")
                ui.label(f"BETA Version {_APP_VERSION}").classes(
                    "text-xs leading-none font-semibold"
                ).style("color:#4ade80; font-size:9px;")

        ui.element("div").classes("w-px bg-gray-700 self-stretch mx-2")

        # Stat helper — single line: LABEL VALUE, wrapped so both share the same
        # flex baseline and font-mono metrics don't shift the value down.
        def _stat_inline(label: str, init: str = "$—", cls: str = "text-white"):
            with ui.row().classes("items-center gap-1 shrink-0 pr-3"):
                ui.label(label).classes("text-xs text-gray-500 leading-none shrink-0")
                val = ui.label(init).classes(
                    f"text-xs font-mono font-semibold leading-none {cls}"
                )
            return val

        bid_val    = _stat_inline("BID")
        ask_val    = _stat_inline("ASK")
        spread_lbl = ui.label("spr:—").classes("text-xs text-gray-500 pr-3 leading-none")
        ui.element("div").classes("w-px bg-gray-700 self-stretch mx-2")
        bal_val  = _stat_inline("MT5 BAL")
        fm_lbl   = ui.label("").classes("text-xs text-gray-500 pr-3 leading-none")
        ui.element("div").classes("w-px bg-gray-700 self-stretch mx-2")
        eq_val   = _stat_inline("EQUITY")
        eq_sub   = ui.label("").classes("text-xs pr-3 leading-none")

        ui.space()

        # Sparkline
        sparkline = ui.echart({
            "backgroundColor": "transparent",
            "animation": False,
            "grid": {"left": 0, "right": 0, "top": 2, "bottom": 2},
            "xAxis": {"type": "category", "show": False, "data": []},
            "yAxis": {"type": "value",    "show": False, "scale": True},
            "series": [{
                "type": "line", "data": [], "showSymbol": False,
                "lineStyle": {"color": "#00CC88", "width": 1.5},
                "areaStyle": {"color": "rgba(0,204,136,0.12)"},
            }],
        }).classes("w-20 shrink-0").style("height:28px")

        acct_lbl    = ui.label(
            "XAUUSD Gold Live"
            if cfg_module.get("account_env", "demo") == "live"
            else "XAUUSD Simulation"
        ).classes("text-xs text-gray-500 px-2 shrink-0")
        conn_badge  = ui.badge("MT5 —", color="grey").classes("text-xs shrink-0")
        mode_btn = ui.button("LOCAL").props("dense outline size=sm color=amber").classes(
            "text-xs ml-1 shrink-0"
        ).style("min-height:20px; padding:0 8px;").tooltip(
            "Local/Remote sync — switch which node is actively trading"
        )
        pause_badge = ui.badge("⏸ PAUSED", color="orange").classes(
            "text-xs ml-1 shrink-0"
        ).style("display:none")
        news_badge = ui.badge("📰 NEWS EVENT", color="red").classes(
            "text-xs ml-1 shrink-0"
        ).style("display:none")

        # ── Local/Remote mode toggle ─────────────────────────────────────────
        # Replaces the old strategy badge. Switching modes runs the
        # STAND_DOWN/RESUME handshake over the sync channel (see
        # forex_trader/sync/) so exactly one node ever executes new trades.
        # This node's own sub-engines (breakout/bounce/gd_copy) are fully
        # stopped/started as part of the switch; the Telegram reader itself
        # keeps running either way to avoid MTProto reconnect churn, but
        # SimulationEngine.open_trade() refuses every new order while stood
        # down regardless — the reader being "warm" cannot execute anything.
        _mode_switching = [False]

        def _mode_sub_engines():
            from forex_trader.breakout_signal import engine as _bo_m
            from forex_trader.test_signal import engine as _bc_m
            from forex_trader.gd_copy_signal import gd_copy_signal_service as _gd_m
            return _bo_m.get_instance(), _bc_m.get_instance(), _gd_m.get_instance()

        async def _refresh_mode_btn():
            if _mode_switching[0]:
                return
            from forex_trader.sync import client as sync_client
            cli = sync_client.get_instance()
            if not cli or cli.conn_state != "connected":
                mode_btn.text = "LOCAL"
                mode_btn.props("color=grey")
                mode_btn.tooltip(
                    "No remote node connected — trading locally by default. "
                    "Configure one in Settings > Remote Node."
                )
                return
            active = db_module.get_active_trader()
            if active == "local":
                mode_btn.text = "LOCAL"
                mode_btn.props("color=amber")
                mode_btn.tooltip("This machine is actively trading. Click to hand control back to the VPS.")
            else:
                mode_btn.text = "REMOTE"
                mode_btn.props("color=blue")
                mode_btn.tooltip("VPS is actively trading; this is a view-only dashboard. Click to take over.")

        async def _toggle_mode():
            from forex_trader.sync import client as sync_client
            cli = sync_client.get_instance()
            if not cli or cli.conn_state != "connected":
                ui.notify("Not connected to a remote node — configure one in "
                          "Settings > Remote Node first.", type="warning")
                return
            if _mode_switching[0]:
                return
            _mode_switching[0] = True
            mode_btn.text = "SWITCHING…"
            mode_btn.props("color=grey")
            mode_btn.disable()
            try:
                current = db_module.get_active_trader()
                if current != "local":
                    # Remote -> Local: take over. Must succeed on the VPS
                    # side (its ack) before this node starts trading.
                    try:
                        ack = await cli.request_stand_down(timeout=15.0)
                    except Exception as exc:
                        ui.notify(f"VPS did not acknowledge stand-down: {exc}", type="negative")
                        return
                    db_module.set_active_trader("local")
                    bo, bc, gd = _mode_sub_engines()
                    for eng in (bo, bc, gd):
                        if eng is not None and not getattr(eng, "is_running", False):
                            eng.start()
                    _n_open = len(ack.get("open_positions", []))
                    ui.notify(
                        f"Now trading locally. VPS stood down"
                        + (f" ({_n_open} of its position(s) still running to their own SL/TP)" if _n_open else "")
                        + ".", type="positive",
                    )
                else:
                    # Local -> Remote: any already-open local positions keep
                    # being managed by this node's own _monitor_loop (SL/TP,
                    # trailing, etc.) regardless of active_trader — that gate
                    # only blocks *new* signals from opening, it doesn't stop
                    # this node from managing trades it already has open. So
                    # switching doesn't abandon them; the user can also just
                    # close them manually in MT5/the broker terminal directly.
                    engine = get_engine()
                    open_trades = engine.get_open_trades()
                    bo, bc, gd = _mode_sub_engines()
                    for eng in (bo, bc, gd):
                        if eng is not None and getattr(eng, "is_running", False):
                            eng.stop()
                    try:
                        await cli.request_resume(timeout=15.0)
                    except Exception as exc:
                        ui.notify(f"VPS did not acknowledge resume: {exc}", type="negative")
                        # Engines are already stopped locally; leave them stopped
                        # rather than guess whether the VPS actually resumed.
                        db_module.set_active_trader("remote_vps")
                        return
                    db_module.set_active_trader("remote_vps")
                    ui.notify(
                        "Control handed back to the VPS."
                        + (f" ({len(open_trades)} local position(s) still running to their own SL/TP)"
                           if open_trades else "")
                        + " This is now view-only.", type="positive",
                    )
            finally:
                _mode_switching[0] = False
                mode_btn.enable()
                await _refresh_mode_btn()

        mode_btn.on_click(_toggle_mode)

        # ── Pause button ───────────────────────────────────────────────────────
        ui.button(icon="pause_circle", on_click=_pause_dialog.open).classes(
            "ml-2 shrink-0"
        ).style(
            "background:transparent; color:#fbbf24; min-width:32px; min-height:32px; "
            "width:32px; height:32px; padding:0;"
        ).tooltip("Pause / Resume trading")

        # ── Power button ───────────────────────────────────────────────────────
        ui.button(icon="power_settings_new", on_click=_power_dialog.open).classes(
            "ml-1 shrink-0"
        ).style(
            "background:transparent; color:#4ade80; min-width:32px; min-height:32px; "
            "width:32px; height:32px; padding:0;"
        ).tooltip("Power options — Restart / Stop")

        # ── Admin button (far right — only visible when KeyGen is present) ──────
        if ADMIN_AVAILABLE:
            ui.button(icon="admin_panel_settings", on_click=_admin_open_fn).classes(
                "ml-2 shrink-0"
            ).style(
                "background:transparent; color:#facc15; min-width:32px; min-height:32px; "
                "width:32px; height:32px; padding:0;"
            ).tooltip("Admin panel (password protected)")

    _last_profit_seq  = [None]   # None = not yet initialised; int = last seen seq
    # Initial-deposit cache: sum of all MT5 balance-credit deals.
    # Fetched once on startup then refreshed every 5 minutes.
    # Used to compute total P&L = equity - net_deposited.
    _net_deposited    = [None]   # None = not yet fetched
    _deposit_fetch_at = [0.0]
    # News event state — tracks when we entered the current news window so we
    # send exactly one Telegram alert per event, not one every 2 seconds.
    _news_in_window      = [False]   # True while inside a news blackout window
    _news_alerted_window = [None]    # window_start timestamp of the last alert sent

    async def _refresh_header():
        try:
            import time as _time
            engine = get_engine()
            tick   = await engine.get_tick()

            # Pause badge
            pause_raw = db_module.get_app_config("trade_pause_until")
            is_paused = pause_raw and float(pause_raw or 0) > _time.time()
            pause_badge.style("" if is_paused else "display:none")

            # Telegram tab dot — an AI-recovered signal is waiting for review
            telegram_ai_badge.set_visibility(db_module.has_unreviewed_ai_recovered_signals())

            # News event badge — check live calendar; send one Telegram alert on entry
            try:
                from forex_trader.test_signal.news_filter import get_current_event as _get_news_event
                _news_ev = _get_news_event()
                if _news_ev:
                    import datetime as _dt_news
                    _resume_utc = _dt_news.datetime.fromtimestamp(
                        _news_ev["window_end"], tz=_dt_news.timezone.utc
                    ).strftime("%H:%M UTC")
                    _pause_mins = int(round(_news_ev["mins_remaining"]))
                    news_badge.text  = f"📰 NEWS EVENT — resumes {_resume_utc}"
                    news_badge.style("")
                    # Send Telegram alert once per unique event window
                    _ws = _news_ev["window_start"]
                    if not _news_in_window[0] or _news_alerted_window[0] != _ws:
                        _news_in_window[0]      = True
                        _news_alerted_window[0] = _ws
                        try:
                            from forex_trader.core import telegram_alerts as _tg
                            import datetime as _dt2
                            _ev_time = _dt2.datetime.fromtimestamp(
                                _news_ev["event_ts"], tz=_dt2.timezone.utc
                            ).strftime("%H:%M UTC")
                            _mins_pre = int(round(max(0, _news_ev["mins_to_event"])))
                            _tg_msg = (
                                f"⚠️ *NEWS EVENT — Trading Paused*\n\n"
                                f"📰 *{_news_ev['title']}* ({_news_ev['currency']})\n"
                                f"🕐 Scheduled: *{_ev_time}*\n"
                                f"⏱ Paused for: *{_pause_mins} minutes*\n"
                                f"✅ Resumes at: *{_resume_utc}*\n\n"
                                f"_All signal generators continue running. "
                                f"No MT5 orders will be placed until the window clears._"
                            )
                            await _tg.send_message(_tg_msg, event_type="news_pause")
                        except Exception:
                            pass
                else:
                    news_badge.style("display:none")
                    _news_in_window[0] = False
            except Exception:
                news_badge.style("display:none")

            if tick:
                prev = _prev_bid[0]
                bid_val.text    = f"${tick.bid:,.2f}"
                ask_val.text    = f"${tick.ask:,.2f}"
                spread_lbl.text = f"spr:{tick.spread_points:.0f}pt"
                if prev is not None:
                    chg = tick.bid - prev
                    col = "text-green-400" if chg >= 0 else "text-red-400"
                    bid_val.classes(replace=f"text-xs font-mono font-semibold {col} pr-3")
                _prev_bid[0] = tick.bid
                _price_hist.append(tick.bid)
                if len(_price_hist) > 60:
                    _price_hist.pop(0)
                sparkline.options["xAxis"]["data"]     = list(range(len(_price_hist)))
                sparkline.options["series"][0]["data"] = list(_price_hist)
                sparkline.update()

            mt5_acc = await engine.get_mt5_account()
            if mt5_acc:
                bal      = float(mt5_acc.get("balance", 0) or 0)
                eq       = float(mt5_acc.get("equity",  0) or 0)
                fm       = float(mt5_acc.get("margin_free", 0) or 0)
                bal_val.text = f"${bal:,.2f}"
                fm_lbl.text  = f"free:${fm:,.0f}"
                eq_val.text  = f"${eq:,.2f}"

                # Refresh initial deposit every 5 minutes.
                # Sum all MT5 balance-credit deals (type=2, positive profit).
                # Net deposits = total credits - total withdrawals.
                import time as _t2
                now_m = _t2.monotonic()
                if _net_deposited[0] is None or now_m - _deposit_fetch_at[0] > 300:
                    try:
                        all_deals = await engine._bridge.get_deal_history(3650)
                        credits = sum(
                            float(d.get("profit", 0))
                            for d in all_deals
                            if d.get("type") == 2 and float(d.get("profit", 0)) > 0
                        )
                        debits = sum(
                            abs(float(d.get("profit", 0)))
                            for d in all_deals
                            if d.get("type") == 2 and float(d.get("profit", 0)) < 0
                        )
                        if credits > 0:
                            _net_deposited[0] = credits - debits
                            _deposit_fetch_at[0] = now_m
                    except Exception:
                        pass

                if _net_deposited[0]:
                    total_pnl = eq - _net_deposited[0]
                    col = "text-green-400" if total_pnl >= 0 else "text-red-400"
                    eq_sub.text = f"P&L:${total_pnl:+.2f}"
                    eq_sub.classes(replace=f"text-xs pr-3 {col}")

            health = await engine.get_bridge_health()
            if health.get("connected"):
                if health.get("trade_allowed") is False:
                    conn_badge.props("color=orange")
                    conn_badge.text = "MT5: AutoTrading OFF"
                    conn_badge.tooltip(
                        "AutoTrading is disabled in MetaTrader 5. "
                        "Click the AutoTrading button (robot icon) in the MT5 toolbar to enable it "
                        "before placing trades."
                    )
                else:
                    conn_badge.props("color=green")
                    conn_badge.text = "MT5 Connected"
            else:
                conn_badge.props("color=red")
                conn_badge.text = "MT5 Disconnected"

            await _refresh_mode_btn()

            cfg = cfg_module.load()
            env = cfg.get("account_env", "demo")
            acct_lbl.text = (
                "XAUUSD Gold Live" if env == "live" else "XAUUSD Simulation"
            )

            # Play cash register sound when a trade closes with profit.
            # On the first tick we silently snapshot the current count so the
            # sound only fires for profitable closes that happen *during* this
            # browser session, not for historical ones.
            current_profit_seq = engine._profit_sound_seq
            if _last_profit_seq[0] is None:
                _last_profit_seq[0] = current_profit_seq
            elif current_profit_seq > _last_profit_seq[0]:
                _last_profit_seq[0] = current_profit_seq
                await ui.run_javascript(_CASH_REGISTER_JS)

        except Exception:
            pass

    ui.timer(2.0, _refresh_header)

    # ── Demo / Live env-switch ────────────────────────────────────────────────
    _cur_env   = cfg_module.get("account_env", "demo")
    _is_live   = [_cur_env == "live"]   # mutable — updated after confirmed switch
    _reverting = [False]                # guards against recursive toggle events

    # ── No-credentials dialog (generic) ──────────────────────────────────────
    with ui.dialog() as _no_creds_dialog, ui.card().classes(
        "bg-gray-800 p-5 rounded-lg max-w-md"
    ):
        _no_creds_title = ui.label("").classes("text-lg font-bold text-yellow-300 mb-2")
        _no_creds_body  = ui.label("").classes(
            "text-sm text-gray-300 mb-4 whitespace-pre-line leading-relaxed"
        )
        ui.button("OK", on_click=_no_creds_dialog.close).classes(
            "bg-gray-700 text-white px-4 py-2"
        )

    # ── MT5 account reminder dialog — shown after successful switch ───────────
    with ui.dialog() as _mt5_remind_dialog, ui.card().classes(
        "bg-gray-800 p-6 rounded-lg max-w-md"
    ):
        _mt5_remind_title = ui.label("").classes("text-lg font-bold mb-2")
        _mt5_remind_body  = ui.label("").classes(
            "text-sm text-gray-300 mb-3 leading-relaxed whitespace-pre-line"
        )
        _mt5_remind_srv   = ui.label("").classes(
            "text-xs font-mono bg-gray-900 rounded p-2 mb-4 text-yellow-300"
        )

        async def _mt5_remind_ok():
            _mt5_remind_dialog.close()
            await asyncio.sleep(0.2)
            await ui.run_javascript("window.location.reload()")

        ui.button(
            "OK — I've switched the account", icon="check",
            on_click=_mt5_remind_ok,
        ).classes("bg-green-700 text-white px-4 py-2 w-full")

    # ── Live confirmation dialog ──────────────────────────────────────────────
    with ui.dialog() as _live_confirm_dialog, ui.card().classes(
        "bg-gray-800 p-5 rounded-lg max-w-md"
    ):
        ui.label("Switch to LIVE Trading").classes(
            "text-lg font-bold text-red-400 mb-2"
        )
        ui.label(
            "This will reconnect the MT5 bridge to your LIVE Vantage Markets account "
            "and switch all data to the live database.\n\nReal funds will be at risk."
        ).classes("text-sm text-gray-300 mb-4 whitespace-pre-line leading-relaxed")

        async def _confirm_live():
            _live_confirm_dialog.close()
            await _do_env_switch("live")

        def _cancel_live():
            _live_confirm_dialog.close()
            _reverting[0] = True
            env_switch.value = False
            _reverting[0] = False

        with ui.row().classes("gap-2"):
            ui.button(
                "Confirm — Switch to Live", icon="warning",
                on_click=_confirm_live,
            ).classes("bg-red-700 text-white px-4 py-2")
            ui.button("Cancel", on_click=_cancel_live).classes(
                "bg-gray-700 text-white px-4 py-2"
            )

    async def _do_env_switch(new_env: str):
        """Switch env: save config + swap DB, reconnect bridge, show MT5 reminder, reload."""
        engine = get_engine()
        # Always read from master credential store (demo DB) — env-independent
        creds = db_module.get_mt5_credentials()

        if new_env == "live":
            login    = int(creds.get("live_login") or 0)
            password = creds.get("live_password_enc") or ""
            server   = (creds.get("live_server") or "").strip()
        else:
            login    = int(creds.get("login") or 0)
            password = creds.get("password_enc") or ""
            server   = (creds.get("server") or "").strip()

        if not login or not password or not server:
            env_label = "Live" if new_env == "live" else "Demo"
            _no_creds_title.text = f"{env_label} Credentials Not Configured"
            _no_creds_body.text  = (
                f"No {env_label} MT5 credentials are saved.\n\n"
                "Go to Settings > MT5 / Bridge, enter your "
                f"{env_label} account login, password and server, "
                "then try switching again."
            )
            _no_creds_dialog.open()
            _reverting[0] = True
            env_switch.value = _is_live[0]
            _reverting[0] = False
            return

        # 1. Swap DB and persist new env immediately
        from forex_trader.config import DATA_DIR as _DATA_DIR
        db_module.init(str(_DATA_DIR / f"forex_trader_{new_env}.db"))
        cfg_module.save_to_yaml({"account_env": new_env})
        _is_live[0] = (new_env == "live")

        # 2. Write bridge_credentials.json so bridge connects correctly on restart
        db_module.sync_bridge_credentials_file(new_env)

        # 3. Tell the bridge to switch to the target account, auto-recover if needed
        bridge_note      = ""
        autotrading_note = ""
        try:
            result = await engine._bridge.send_credentials(login, password, server)
            if result.get("status") == "connected":
                # /credentials already calls enable_autotrading() internally.
                # If it still failed (bridge older build or Win32 error), show a note.
                at = result.get("autotrading") or {}
                if not at.get("enabled") and not result.get("trade_allowed"):
                    # Older bridge builds don't return autotrading field — try explicitly
                    if "autotrading" not in result:
                        at = await engine._bridge.enable_autotrading()
                    if not at.get("enabled"):
                        at_err = at.get("error", "unknown")
                        autotrading_note = (
                            f"\n\nAlgo Trading could not be enabled automatically ({at_err}). "
                            "Please open MetaTrader 5 and click the AutoTrading (robot) button "
                            "in the toolbar."
                        )
            else:
                # Bridge saved credentials but MT5 login failed — try /reconnect
                err = result.get("error") or result.get("status") or "unknown"
                try:
                    reconnect_result = await engine._bridge.reconnect()
                    if reconnect_result.get("status") == "connected":
                        bridge_note = "\n\nBridge reconnected successfully."
                        at = await engine._bridge.enable_autotrading()
                        if not at.get("enabled"):
                            at_err = at.get("error", "unknown")
                            autotrading_note = (
                                f"\n\nAlgo Trading could not be enabled automatically ({at_err}). "
                                "Please open MetaTrader 5 and click the AutoTrading (robot) "
                                "button in the toolbar."
                            )
                    else:
                        reconnect_err = reconnect_result.get("error", "unknown error")
                        bridge_note = (
                            f"\n\nBridge could not reconnect to MT5: {reconnect_err}.\n"
                            "Go to Settings > MT5 / Bridge and click Start Bridge."
                        )
                except Exception as reconnect_exc:
                    bridge_note = (
                        f"\n\nBridge is offline: {reconnect_exc}.\n"
                        "Go to Settings > MT5 / Bridge and click Start Bridge."
                    )
        except Exception as _be:
            bridge_note = (
                f"\n\nCould not reach the bridge: {_be}.\n"
                "Go to Settings > MT5 / Bridge and click Start Bridge if it is not running."
            )

        # 4. Show MT5 account reminder — user clicks OK to trigger reload
        extra = bridge_note + autotrading_note
        if new_env == "live":
            _mt5_remind_title.text = "Switched to LIVE Trading"
            _mt5_remind_title.classes(replace="text-lg font-bold text-red-400 mb-2")
            _mt5_remind_body.text = (
                "The app is now in LIVE mode.\n\n"
                "Please ensure MetaTrader 5 is logged into your "
                "LIVE account before clicking OK. "
                "All displayed data will reflect the live account once you proceed."
                + extra
            )
        else:
            _mt5_remind_title.text = "Switched to Simulation / Demo"
            _mt5_remind_title.classes(replace="text-lg font-bold text-blue-400 mb-2")
            _mt5_remind_body.text = (
                "The app is now in DEMO / Simulation mode.\n\n"
                "Please ensure MetaTrader 5 is logged into your "
                "DEMO account before clicking OK."
                + extra
            )
        _mt5_remind_srv.text = f"MT5 Server:  {server}"
        _mt5_remind_dialog.open()

    # ── Tab navigation row  (switch ← | tabs) ────────────────────────────────
    with ui.row().classes(
        "w-full bg-gray-900 border-b border-gray-700 items-stretch"
    ).style("min-height:48px"):

        # Demo / Live toggle — far left
        with ui.row().classes(
            "items-center gap-1.5 px-3 shrink-0"
        ).style("border-right:1px solid #374151"):
            demo_lbl = ui.label("Demo").classes(
                "text-xs font-semibold leading-none "
                + ("text-blue-400" if not _is_live[0] else "text-gray-600")
            )
            env_switch = ui.switch("").props("dense color=red").style(
                "transform:scale(0.85);"
            )
            env_switch.value = _is_live[0]
            live_lbl = ui.label("Live").classes(
                "text-xs font-semibold leading-none "
                + ("text-red-400" if _is_live[0] else "text-gray-600")
            )

        def _on_env_switch(e):
            if _reverting[0]:
                return
            # NiceGUI 3.x on_value_change gives ValueChangeEventArguments with .value
            new_live = bool(e.value)
            new_env  = "live" if new_live else "demo"
            if new_env == cfg_module.get("account_env", "demo"):
                return
            if new_live:
                _live_confirm_dialog.open()
            else:
                asyncio.ensure_future(_do_env_switch("demo"))

        # Use on_value_change (NiceGUI 3.x API) — on("update:model-value") passes e.value=None
        env_switch.on_value_change(_on_env_switch)

        # Tabs fill the rest of the row
        with ui.tabs().classes("flex-1 bg-transparent") as tabs:
            tab_ai       = ui.tab("AI Analysis", icon="smart_toy")
            tab_chart    = ui.tab("Chart",       icon="candlestick_chart")
            tab_trading  = ui.tab("Trading",     icon="trending_up")
            tab_telegram = ui.tab("Telegram",    icon="send")
            with tab_telegram:
                # Small red dot — shown whenever an AI-recovered signal is
                # waiting for review/approval in Reader Logic > AI (see
                # _refresh_header below for the toggle).
                telegram_ai_badge = ui.badge().props("floating dot color=red")
                telegram_ai_badge.set_visibility(False)
            tab_test     = ui.tab("Signal Generator", icon="science")
            tab_edge     = ui.tab("Edge",        icon="insights")
            tab_backtest = ui.tab("Backtest",    icon="bar_chart")
            tab_history  = ui.tab("History",     icon="history")
            tab_settings = ui.tab("Settings",    icon="settings")
            tab_about    = ui.tab("About",       icon="info")

        # ── Circuit breaker — global, live-trades-only indicator ────────────
        # The only circuit breaker that ever blocks a real MT5 order (see
        # core/database.py's get_circuit_breaker_state/record_live_trade_
        # outcome) — signal generators used to each show their own separate
        # virtual-P&L threshold badge, which had nothing to do with live
        # execution and just duplicated/confused this one. Single indicator,
        # always visible, next to About.
        with ui.row().classes(
            "items-center gap-1.5 px-3 shrink-0"
        ).style("border-left:1px solid #374151"):
            cb_icon = ui.icon("shield", size="xs").classes("text-green-400")
            cb_top_lbl = ui.label("Circuit Breaker OK").classes(
                "text-xs font-semibold leading-none text-green-400"
            )

        def _refresh_cb_badge():
            try:
                cb = db_module.get_circuit_breaker_state()
            except Exception:
                return
            if cb.get("is_active"):
                rem = int(cb.get("remaining_secs", 0))
                hms = f"{rem // 3600:02d}:{(rem % 3600) // 60:02d}:{rem % 60:02d}"
                cb_icon.classes(replace="text-red-400")
                cb_icon.props("name=block")
                cb_top_lbl.text = f"Circuit Breaker Active — resumes in {hms}"
                cb_top_lbl.classes(replace="text-xs font-semibold leading-none text-red-400")
            else:
                cb_icon.classes(replace="text-green-400")
                cb_icon.props("name=shield")
                cb_top_lbl.text = "Circuit Breaker OK"
                cb_top_lbl.classes(replace="text-xs font-semibold leading-none text-green-400")

        ui.timer(5.0, _refresh_cb_badge)
        _refresh_cb_badge()

    def _on_tab_change(e):
        if e.value != "History":
            return
        # Only celebrate if today's closed-trade P&L is positive
        cutoff = _time.time() - 86400
        try:
            with db_module.db() as conn:
                row = conn.execute(
                    "SELECT COALESCE(SUM(COALESCE(mt5_profit, net_pnl, 0)), 0) "
                    "FROM vantage_simulated_trades "
                    "WHERE status='closed' AND close_time > ?",
                    (cutoff,),
                ).fetchone()
            daily_pnl = float(row[0]) if row and row[0] is not None else 0.0
        except Exception:
            daily_pnl = 0.0
        if daily_pnl <= 0:
            return
        ui.run_javascript("""
                (function() {
                    const gold   = ['#f59e0b', '#fbbf24', '#fde68a', '#d97706'];
                    const colors = ['#f59e0b', '#10b981', '#60a5fa', '#a78bfa', '#f472b6', '#fbbf24'];
                    const opts = {
                        particleCount: 80,
                        spread: 100,
                        startVelocity: 45,
                        ticks: 200,
                        colors: colors,
                        origin: { y: 0.75 }
                    };
                    // Centre burst
                    confetti({ ...opts, origin: { x: 0.5, y: 0.75 } });
                    // Left cannon after 400 ms
                    setTimeout(() => confetti({
                        particleCount: 60, angle: 60, spread: 70,
                        startVelocity: 50, ticks: 180,
                        colors: gold, origin: { x: 0.05, y: 0.8 }
                    }), 400);
                    // Right cannon after 400 ms
                    setTimeout(() => confetti({
                        particleCount: 60, angle: 120, spread: 70,
                        startVelocity: 50, ticks: 180,
                        colors: gold, origin: { x: 0.95, y: 0.8 }
                    }), 400);
                    // Second centre burst at 1.4 s
                    setTimeout(() => confetti({
                        particleCount: 50, spread: 120,
                        startVelocity: 35, ticks: 150,
                        colors: colors, origin: { x: 0.5, y: 0.6 }
                    }), 1400);
                    // Final wide shower at 2.2 s
                    setTimeout(() => confetti({
                        particleCount: 70, spread: 160,
                        startVelocity: 25, ticks: 120,
                        colors: colors, origin: { x: 0.5, y: 0.5 },
                        gravity: 0.6, scalar: 0.9
                    }), 2200);
                })();
            """)

    tabs.on_value_change(_on_tab_change)

    # animated=False — see the same fix on the Signal Generator sub-tabs
    # (test_panel.py) for why: Quasar's slide transition can get stuck showing
    # the previous tab's content indefinitely after content elsewhere on the
    # page rebuilds, not just as a brief flash.
    with ui.tab_panels(tabs, value=tab_chart).props("animated=false").classes("w-full flex-1 bg-gray-900"):
        with ui.tab_panel(tab_ai):
            ai_summary_page.render(get_engine)
        with ui.tab_panel(tab_chart):
            chart_page.render(get_engine)
        with ui.tab_panel(tab_trading):
            trading.render(get_engine, get_tg_reader)
        with ui.tab_panel(tab_telegram):
            telegram.render(get_tg_reader)
        with ui.tab_panel(tab_history).style("padding:0"):
            history.render(get_engine)
        with ui.tab_panel(tab_settings):
            settings_page.render(get_engine, get_tg_reader)
        with ui.tab_panel(tab_backtest):
            backtest_page.render(get_engine)
        with ui.tab_panel(tab_about):
            _render_about()
        with ui.tab_panel(tab_test):
            test_panel.render(get_engine)
        with ui.tab_panel(tab_edge):
            edge_dashboard.render(get_engine)
