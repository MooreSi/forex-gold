/**
 * Setup instructions, per platform and then the parts common to all of them.
 *
 * Ported from the NiceGUI About page (`frontend/app/_about.py`) on
 * 2026-09-21 at the owner's request — it was never carried over, so a fresh
 * install had nothing in the app telling it how to get MT5, the bridge, the
 * EA, Telegram or the licence working.
 *
 * **Brought up to date rather than transcribed.** Every Settings path in the
 * original names a NiceGUI page that no longer exists, and two of the port
 * numbers changed with the fork. What is written here was checked against
 * this build:
 *
 * | Original said | This build |
 * |---|---|
 * | Settings > Bridge & Config (Anthropic key) | Settings → AI |
 * | Settings > Telegram / Telegram Reader | Settings → Connections |
 * | Settings > Email Reports | Settings → Connections → Email |
 * | Settings > Registration | Settings → Access & licence |
 * | Settings > Remote Node | Settings → Remote node |
 * | History tab | Analysis tab |
 * | bridge on 9000 | 9010 (`mt5_bridge_url`) |
 * | EA bridge on 9101 | 9111 (`ea_bridge_port`) |
 *
 * The two ports moved deliberately: this build and the original can be
 * installed on one machine, and a shared port would let one app's engine
 * trade through the other's bridge. See `backend/src/config/__init__.py`.
 */

export interface SetupCard {
  title: string;
  steps: string[];
}

export const WINDOWS_SETUP: SetupCard[] = [
  {
    title: "1. Windows — MetaTrader 5",
    steps: [
      "No compatibility layer is needed on Windows — MT5 and the app both run natively.",
      "Download MetaTrader 5 from the Vantage Markets website (or metatrader5.com) and run the installer.",
      "Open MetaTrader 5 and log in with your Vantage Markets demo or live credentials (server: VantageMarkets-Demo or VantageMarkets-Live).",
      "Keep MetaTrader 5 open whenever you use this app. On Windows the app connects to it in-process — there is no separate bridge program to install or start.",
      "Enable Algo Trading in the MT5 toolbar: click the robot icon so it turns green. This must be ON for the app to place any trade. MT5 turns it off again after a terminal restart or an account switch, so re-enable it each time you reopen MT5.",
      "Once a terminal path is saved in Settings → MT5, 'Setup & Start FOREX.bat' launches MT5 for you on startup if it is not already running.",
    ],
  },
  {
    title: "2. Windows — running the app",
    steps: [
      "Double-click 'Setup & Start FOREX.bat' in the project folder to install and launch the app — no CrossOver, Wine or manual Python setup required.",
      "On first run it locates or installs Python 3.11+, creates a virtual environment and installs the dependencies. That takes about a minute and only repeats when the dependencies change.",
      "The app then opens in your browser at http://localhost:8888.",
      "Leave the command window open while the app is running — closing it stops the app. To stop it deliberately, use 'Stop FOREX.bat', which also pauses the keep-alive watchdog so Stop genuinely stops.",
    ],
  },
];

export const MAC_SETUP: SetupCard[] = [
  {
    title: "1. macOS — CrossOver",
    steps: [
      "CrossOver is needed to run MetaTrader 5, which is a Windows application: MT5 has no native Mac build, and Apple Silicon Macs cannot run the MetaTrader5 Python package directly.",
      "Go to codeweavers.com and buy or trial CrossOver.",
      "Download and install the CrossOver .dmg, then open CrossOver from Applications.",
      "CrossOver creates a 'bottle' — a Windows compatibility layer. MetaTrader 5 is installed into that bottle.",
    ],
  },
  {
    title: "2. macOS — MetaTrader 5 in CrossOver",
    steps: [
      "In CrossOver, click 'Install a Windows Application'.",
      "Search for 'MetaTrader 5' — CrossOver has a built-in installer for it.",
      "Follow the installer and let it create a bottle named 'MetaTrader 5'.",
      "Open MetaTrader 5 from within CrossOver and log in with your Vantage Markets demo or live credentials (server: VantageMarkets-Demo or VantageMarkets-Live).",
      "Keep MetaTrader 5 open whenever you use this app — the bridge in the next section needs it.",
      "Enable Algo Trading in the MT5 toolbar: click the robot icon so it turns green. This must be ON for the app to place any trade, and MT5 turns it off again after a terminal restart or an account switch.",
    ],
  },
  {
    title: "3. macOS — the MT5 bridge",
    steps: [
      "The bridge (mt5_bridge.py) is a small Python server that runs inside the CrossOver bottle and translates this app's calls into MT5 actions. It exists only on macOS, because MT5 runs under Wine there and the app's own Python cannot import the MetaTrader5 package. Windows does not need it at all.",
      "Start it from the power button in the top bar, or by double-clicking 'Start MT5 Bridge.command' in the project folder. 'FOREX Start.command' starts the app and the bridge together.",
      "The bridge must be running whenever you use any trading feature. It listens on the address in Settings → MT5 — http://localhost:9010 in this build.",
      "If the bridge stops, the app's own bridge watchdog usually reconnects or restarts it. Settings → MT5 shows its live status; Settings → Node & updates has the keep-alive switch that restarts the whole app if it goes down.",
    ],
  },
];

export const VPS_SETUP: SetupCard[] = [
  {
    title: "1. VPS — Windows and MT5",
    steps: [
      "A VPS running FOREX Trader is just a headless Windows machine: MT5 and the app run natively, exactly as they do on a desktop Windows install. Connect over Remote Desktop (RDP) to do the setup below.",
      "Download MetaTrader 5 from the Vantage Markets website and log in with your demo or live credentials.",
      "Enable Algo Trading (robot icon → green) in the MT5 toolbar. Re-enable it after every MT5 restart or account switch.",
      "There is no separate bridge program to install on Windows — the app connects to MT5 in-process.",
    ],
  },
  {
    title: "2. VPS — firewall rules",
    steps: [
      "Open Windows Defender Firewall with Advanced Security on the VPS, and your hosting provider's own network security group if it has one in front of it.",
      "Allow inbound Remote Desktop (TCP 3389) so you can manage the VPS at all. Restrict the allowed source address to your own where you can.",
      "Allow inbound TCP 8765 — the sync server port the paired Mac connects to for Local/Remote pairing (Settings → Remote node). It is the only FOREX Trader port the VPS needs to accept from outside, and the connection is TLS-encrypted and token-authenticated.",
      "Do not open port 8888 to the internet. That is the dashboard, and it has no login of its own. The app binds it to 127.0.0.1 by default so it is unreachable from the network even if a rule exists — reach it over RDP and browse to http://localhost:8888 inside the session. Widen the bind only once real authentication is in front of it.",
      "The MT5 bridge port (9010) and the EA bridge port (9111) never need a firewall rule: the bridge is not used on native Windows, and the EA bridge only ever talks to itself on localhost.",
    ],
  },
  {
    title: "3. VPS — running headless",
    steps: [
      "A VPS is meant to run unattended. Before you disconnect the RDP session, enable Headless mode in Settings → Remote node, so the app stops trying to open a browser window that has nowhere to display.",
      "Turn on 'Restart the app automatically if it stops' in Settings → Node & updates. It registers a Task Scheduler entry that checks every couple of minutes that the app is still serving on its port and starts it again if it is not — including after a reboot.",
      "Set MetaTrader 5 to auto-login on startup in its own options, so MT5 and FOREX Trader come back together after a reboot without you having to RDP in.",
    ],
  },
  {
    title: "4. VPS — pairing with your Mac or PC",
    steps: [
      "On the VPS, go to Settings → Remote node, enable 'This machine is the VPS', and note the fingerprint and the shared token it shows.",
      "On your Mac, go to Settings → Remote node → 'Connect to a remote VPS', enter the VPS's address and the shared token, then Save & Connect.",
      "Use the Local/Remote control in the header to choose which node actually places trades. Only one may be the active trader: both receive every Telegram signal, and the one that is not active stands down from placing orders, so the shared MT5 account never gets duplicates.",
      "The Signal Generator panels mirror whichever node is active. With the VPS active, the Mac shows the VPS's balance, stats and circuit-breaker state, and Start/Stop/Run Now act on the VPS rather than on the Mac's stood-down copy.",
    ],
  },
];

export const COMMON_SETUP: SetupCard[] = [
  {
    title: "Expert Advisor (EA bridge) — optional",
    steps: [
      "Optional, and off by default. With it off, Python manages every trade exactly as it always has and none of this is needed. With it on, a companion MQL5 Expert Advisor places and manages eligible strategies' orders inside MT5's own tick loop, which reacts faster than Python's roughly one-second cycle. DPM stays Python-managed either way, because it needs calibration data only the app holds.",
      "In MT5, File → Open Data Folder, then MQL5 → Experts. Copy 'ForexTraderBridge.mq5' from the project's mql5 folder into it.",
      "Open MetaEditor (F4), find ForexTraderBridge.mq5 in the Navigator and press Compile (F7). That produces ForexTraderBridge.ex5 beside it — once per terminal, repeated only when the .mq5 source changes.",
      "Back in MT5, right-click Expert Advisors in the Navigator, choose Refresh, then drag 'ForexTraderBridge' onto the XAUUSD chart.",
      "In the dialog that opens, on the Common tab, tick 'Allow Algo Trading' for this EA — separate from, and in addition to, the terminal-wide toggle in the toolbar.",
      "One terminal setting is required or the EA can never connect: Tools → Options → Expert Advisors, tick 'Allow WebRequest for listed URL', and add 127.0.0.1 to the list.",
      "In the app, Settings → MT5, turn on the EA bridge and save. The EA's InpPort input must match the app's EA bridge port — 9111 in this build. The app hands a trade to the EA only when it can see a live, connected EA on that terminal; otherwise Python manages it as before, with nothing else to change.",
      "To confirm it is working, open a small test trade with the EA attached and the switch on. The EA's own activity appears in MT5's Experts tab beside the position, and the header's EA badge turns green.",
    ],
  },
  {
    title: "Telegram bot (trade alerts)",
    steps: [
      "The bot sends you a message when a trade opens, hits a target, or closes.",
      "In Telegram, search for @BotFather. Send /newbot and follow the prompts.",
      "BotFather gives you a token shaped like 1234567890:ABCdef… — copy it.",
      "For your chat ID: message your new bot, then message @userinfobot or @RawDataBot, which replies with it.",
      "Put the token and chat ID into Settings → Connections → Telegram alerts, tick it on and save. 'Test alert' there confirms delivery.",
    ],
  },
  {
    title: "Telegram reader (signal channels)",
    steps: [
      "The reader watches Telegram channels for signals in real time. It needs your personal Telegram API credentials — not a bot token.",
      "Go to my.telegram.org/apps and sign in with your Telegram phone number.",
      "Click 'Create new application'. Any app name and short name will do (e.g. 'FOREX Reader'); the platform can be 'Desktop'.",
      "You are given an API ID (a number) and an API hash (a long string). Copy both.",
      "Enter them in Settings → Connections → Telegram reader with your phone number in international format (+441234567890), and save.",
      "On the Parsing tab, click Authenticate and enter the code Telegram sends you.",
      "Once authenticated, load the groups and assign the channels you want to the slots. Each channel's parsing, keywords and strategy are set on that same tab.",
    ],
  },
  {
    title: "Resend (email reports)",
    steps: [
      "Resend is the recommended provider: it works over HTTPS, with no SMTP setup, app passwords or firewall problems.",
      "Sign up at resend.com — the free tier is 3,000 emails a month and needs no card.",
      "In the Resend dashboard, API Keys → Create API Key. Any name will do.",
      "Copy the key (it starts with re_) into Settings → Connections → Email → Resend API key.",
      "Set the address reports go to in the same section, choose the daily or weekly summary and the send time, and save.",
      "Use 'Test delivery' to confirm it arrives before you rely on the scheduled reports.",
    ],
  },
  {
    title: "Anthropic API key (AI features)",
    steps: [
      "An Anthropic key enables Claude's trade commentary and the AI Analysis tab. Nothing about trading needs it — with no key, the AI surfaces say so and the rest of the app is unaffected.",
      "Go to console.anthropic.com and sign in or create an account.",
      "API Keys → Create Key. Give it a name such as 'FOREX Trader'.",
      "Paste it into Settings → AI and save. The key is stored encrypted outside the database — the screen can only tell you whether one is set, never show it back.",
      "Claude Sonnet is the default: the balance of speed and quality. Every AI action that costs money says so on its button before you press it.",
    ],
  },
  {
    title: "Licence key",
    steps: [
      "A licence is required. Without one the app shows an activation screen at launch instead of starting.",
      "Your Machine ID is detected automatically and shown on that screen; your administrator uses it to generate the licence.",
      "Enter your name and email and click Request approval. That sends the request to the licence server, and the app activates itself once the administrator approves — nothing else to do.",
      "If the licence server cannot be reached, use Manual activation and paste the key you were sent directly, in the form KEY|EXPIRY_DATE.",
      "The licence is tied to the Machine ID. Reinstalling on a new machine needs a new key from your administrator.",
      "Once activated, Settings → Access & licence shows the email, the masked key, the machine ID, the licence type and the days remaining, read-only.",
    ],
  },
  {
    title: "Going live",
    steps: [
      "Before switching to live trading, test every credential, risk setting and strategy on the demo account.",
      "In Settings → MT5, fill in the live login, password and server (VantageMarkets-Live).",
      "Switch the environment control in the header from Demo to Live. It asks you to confirm, and the header turns to the live colour so the two are never confused at a glance.",
      "Start conservatively: 0.5% or less risk per trade and a max lot size, until you trust the setup.",
      "Watch the first live session closely. The Analysis tab has every closed trade, the equity curve and the channel scorecard.",
    ],
  },
];

export const PLATFORMS: { id: string; label: string; cards: SetupCard[] }[] = [
  { id: "windows", label: "Windows", cards: WINDOWS_SETUP },
  { id: "mac", label: "Mac", cards: MAC_SETUP },
  { id: "vps", label: "VPS", cards: VPS_SETUP },
];
