"""The About tab: what the app is, how to set it up, and the release notes."""
from typing import Optional

from nicegui import ui

from backend.src.utils.version_history import __version__ as _APP_VERSION


def _render_about(nav: Optional[dict] = None):
    _sub: list[str] = ["home"]   # mutable so nested functions can mutate

    # ── Sub-page content containers ───────────────────────────────────────────
    content_area = ui.column().classes("w-full")

    def _show_home():
        _sub[0] = "home"
        content_area.clear()
        with content_area:
            # "Set up once / Every day" — frontend/components/about_home.py
            from frontend.components import about_home
            about_home.render(_show_section, _APP_VERSION)

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
                                "Do not open port 8888 to the internet — that's the app's own dashboard and it has no separate login of its own. By default the app now binds the dashboard to localhost (127.0.0.1) only, so it is not reachable from the network even if a firewall rule exists; access it via RDP, then browse to http://localhost:8888 inside the remote desktop session. Only widen the bind (config 'host') once real authentication is in front of it.",
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
                                "Signal generator panels (Bounce, Breakout, Reversal Engine) mirror whichever node is active: when the VPS is active, the Mac's own panels show the VPS's real balance, stats and Circuit Breaker status, and Start/Stop/Run Now/Reset buttons act on the VPS instead of the Mac's stood-down local copy.",
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

                    from backend.src.utils.version_history import RELEASES as releases

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

                elif section == "glossary":
                    _sub_header("Glossary", "translate")
                    ui.label(
                        "Every trading term and app-specific concept used across FOREX Trader, "
                        "in plain English."
                    ).classes("text-xs text-gray-500 -mt-2 mb-1")

                    GLOSSARY_SECTIONS = [
                        ("Order & Risk Basics", [
                            ("SL — Stop Loss", "The price at which a losing trade closes automatically to cap the loss. Every trade has one."),
                            ("TP — Take Profit", "A target price at which some or all of a trade closes for profit. Trades can have up to 8 TP levels (TP1–TP8), closed in stages."),
                            ("R:R — Risk:Reward", "How many multiples of the risk taken a trade actually returned. Shown as e.g. \"2.5:1\" or \"+2.5R\" — a trade risking $50 that banked $125 is 2.5R. Negative means it lost money relative to what was risked."),
                            ("Realized R", "The R:R actually achieved by a closed trade (net P&L ÷ dollar risk taken at entry), as opposed to a plan ratio computed before the trade happened."),
                            ("BE — Breakeven", "Moving the Stop Loss to the entry price once a trade is in enough profit, so the trade can no longer lose money even if price reverses."),
                            ("Pip / Point", "A unit of price movement. For XAUUSD (gold) in this app, 1 point = $0.01 of price movement; a $10.00 move is 1,000 points."),
                            ("Lot", "The trade size. 0.10 lots on XAUUSD means $10 profit or loss per 1-point price move (1 lot = 100 oz)."),
                            ("Spread", "The gap between the buy (ask) and sell (bid) price at any moment — an implicit cost paid on every trade."),
                            ("Drawdown", "How far equity has fallen from its most recent peak, shown as a percentage. Max Drawdown is the worst such dip over the period shown."),
                            ("Win Rate", "The percentage of closed trades that ended in profit."),
                            ("Profit Factor", "Total profit from winning trades divided by total loss from losing trades. Above 1.0 means profitable overall; below 1.0 means losing overall."),
                        ]),
                        ("Order Types", [
                            ("Market Order", "An order that fills immediately at whatever the current price is."),
                            ("Limit Order (Pending Order)", "An order that rests unfilled on the broker's book until price reaches a specified level, then fills automatically. Used by the Limit Runner strategy."),
                            ("GTC — Good Till Cancelled", "A pending order that stays resting until it either fills or is explicitly cancelled/expires — as opposed to expiring at the end of the trading day."),
                            ("Entry Realignment", "Limit Runner setting: if price has already moved through the signalled zone by the time the order would be placed, enters at market instead and shifts SL/TP by the same distance rather than losing the signal to a broker rejection."),
                        ]),
                        ("Technical Indicators", [
                            ("ADX — Average Directional Index", "Measures how strong a trend is (not its direction). Above ~25 typically indicates a trending market; below suggests a ranging/choppy one. Several strategies require a minimum ADX before entering."),
                            ("EMA — Exponential Moving Average", "A trend line that weights recent price more heavily than older price. This app shows EMA 9/21/50 on the chart."),
                            ("RSI — Relative Strength Index", "A 0–100 momentum indicator. Above 70 is typically considered overbought, below 30 oversold."),
                            ("ATR — Average True Range", "A measure of typical price movement size over recent candles, used to size stops and detect unusually quiet ('collapsed') markets."),
                            ("HTF Bias — Higher-Timeframe Bias", "The prevailing trend direction on a longer timeframe (e.g. H4/H1) than the one a signal fires on, used as a filter or confidence signal."),
                        ]),
                        ("Automation & Risk Controls", [
                            ("DPM — Dynamic Position Management", "Replaces fixed-TP management with continuous live monitoring: trails the stop as price moves favourably, banks partial profit at key levels, and can close early on a reversal beyond a configured threshold."),
                            ("IME — Immediate Market Entry", "Reads channels for bare 'Buy Now'/'Sell Now' messages and enters at current market price immediately, updating SL/TP automatically when the full signal follows."),
                            ("Auto-Execution", "When on, incoming Telegram signals are traded automatically. When off, they're recorded but require manual execution."),
                            ("Circuit Breaker", "An automatic trading pause triggered after a run of consecutive losses, to stop a losing streak from compounding until manually reset or the cooldown elapses."),
                            ("Kelly Criterion Sizing", "Adjusts live lot size using a fraction (half-Kelly) of the mathematically optimal bet size, based on rolling win rate and R:R. Clamped to a modest ±25% adjustment."),
                            ("Trading Schedule", "A per-day, per-time-window profit target — once a window's target is hit, no further automated entries fire in that window for the rest of the day."),
                            ("Signal", "A parsed trade idea (direction, entry, SL, TPs) from a Telegram channel or generated internally — not yet a trade until it executes."),
                            ("Channel Strategy", "Which management strategy a given Telegram channel's signals use — set per channel, or left on Auto for Claude to recommend one based on that channel's own track record."),
                        ]),
                        ("EA Template Terms", [
                            ("EA — Expert Advisor", "The native MetaTrader 5 program (ForexTraderBridge.mq5) that can manage a trade directly inside MT5 once handed off from this app, so management continues even if this app briefly disconnects."),
                            ("EA Template", "A saved, reusable set of entry/management rules (Anchor, Trail, Grid, Stealth, Breakeven, Harvest settings) that fully replaces a channel's normal strategy — the EA runs it natively."),
                            ("Anchor", "The reference price a template's grid or trail measures from — typically the price at signal time."),
                            ("Trail", "A stop-loss that follows price at a fixed or rule-based distance as it moves favourably, locking in gains without a fixed target."),
                            ("Grid", "A template mode that places several resting limit orders staggered at fixed intervals, averaging into a position as price moves through each level."),
                            ("Stealth", "A template mode that delays or disguises order placement/management to reduce visible footprint."),
                            ("Cancel Pending", "Grid template setting: once one leg of the grid fills, automatically cancel every other still-resting leg instead of letting them all fill."),
                            ("Harvest", "A template setting that locks in (closes) profit once it crosses a configured threshold, independent of the normal TP ladder."),
                        ]),
                        ("Strategy Names", [
                            ("Scale Out", "Closes a portion of the position at each TP level and moves SL to breakeven after TP1."),
                            ("BE Runner", "Keeps the full position open with no partial closes; SL steps forward to each newly-cleared TP price."),
                            ("Trailing Stop", "SL trails a fixed distance behind price once TP1 is reached, with no fixed final target."),
                            ("Protected Scale", "Holds through TP1 and TP2 (SL moves to breakeven at TP2), then scales out from TP3 onward."),
                            ("Conservative", "Ignores the signal's own SL/TP entirely and uses a fixed, tight 5-point SL / 3-point TP1 from the actual fill price."),
                            ("Conservative Trial", "A variant of Conservative with its own fixed SL/TP/breakeven schedule, used to trial different fixed levels without touching the original Conservative strategy."),
                            ("Scalp Runner", "A tight two-stage scalp: an initial small target confirms the move, then the remaining position trails on a close stop."),
                            ("Signal Climber", "Uses the signal's own full TP ladder (up to TP8), closing a share at each level and stepping SL forward as each one clears."),
                            ("Reversal Runner", "The Reversal Engine's own management style — widens the signal's stop within limits and rides the full TP ladder."),
                            ("Adaptive Runner / Adaptive Runner 2", "Variants of Reversal Runner with different stop-widening rules (capped proportionate to reward, or a flat fixed distance) — built for signal sources with inconsistent TP-ladder quality."),
                            ("Limit Runner", "Places a genuine resting limit order at the broker rather than waiting to fill at market, then manages the position with its own TP ladder once filled."),
                        ]),
                    ]

                    for section_title, terms in GLOSSARY_SECTIONS:
                        with ui.card().classes("w-full bg-gray-800 rounded-lg p-5"):
                            ui.label(section_title).classes(
                                "text-sm font-semibold text-yellow-300 mb-3"
                            )
                            with ui.column().classes("w-full gap-3"):
                                for term, definition in terms:
                                    with ui.column().classes("gap-0.5"):
                                        ui.label(term).classes(
                                            "text-sm font-semibold text-cyan-300"
                                        )
                                        ui.label(definition).classes(
                                            "text-xs text-gray-300 leading-relaxed"
                                        )


    if nav is not None:
        nav["show_section"] = _show_section
        nav["show_home"] = _show_home
    _show_home()
