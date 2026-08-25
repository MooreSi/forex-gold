# FOREX Trader

An automated XAUUSD (gold) trading application. It reads signals from Telegram
channels, applies risk rules, places and manages real orders through
MetaTrader 5, and shows everything in a local web dashboard.

> **This trades real money on a live broker account.**
> If you are an AI agent, read [CLAUDE.md](CLAUDE.md) before changing anything.

---

## Running it

```bash
pip install -r requirements.txt
python run.py
```

The dashboard opens at **http://localhost:8888**.

You will need a licence key on first run, and MT5 credentials configured under
**Settings → MT5**.

| Script | What it does |
|---|---|
| `python run.py` | start the app |
| `Setup & Start FOREX.bat` | Windows: install deps and start |
| `FOREX Start.command` | macOS: start |
| `Start MT5 Bridge.command` | macOS: start the MT5 bridge alone |

## What it does

**Signals in.** Watches configured Telegram channels, parses entry/SL/TP out of
messages (several channel formats, plus an AI fallback for format drift), and
deduplicates reposts and edits.

**Decisions.** A signal must survive staleness checks, logic-keyword filters, a
minimum reward:risk floor, a cap on correlated open trades, session and
schedule gates, and — when enabled — a deterministic Risk Governor that sizes
every position from risk percentage and the real stop distance.

**Orders out.** Places market or pending orders via the MT5 bridge, then
manages each trade to its strategy: scale-out ladders, breakeven runners,
trailing stops, ORB/IVB fixed setups, and more. Take-profit ladders are polled
sub-second because gold levels can sit a point apart.

**Watching.** Reconciles the app's view against what the broker actually holds,
recovers from bridge outages, syncs realised profit, and alerts to Telegram.

**Extras.** Three research engines (breakout, reversal, test-signal), an ORB/IVB
report, AI trade commentary, backtesting, email reports, and an optional paired
Mac + VPS setup where one node trades and the other watches.

## Layout

```
run.py                  launcher
mt5_bridge.py           talks to MetaTrader 5 (separate process, own interpreter)

backend/src/
    app.py              composition root — builds and wires everything
    runtime.py          TradingRuntime: owns the background tasks and shared state
    config/             settings, licence
    db/                 schema, connections, transactions
    services/           all the behaviour, one package per domain
    controllers/        translates between the UI and the services
    utils/              bottom of the stack

frontend/               NiceGUI dashboard — pages only, no database access

tests/                  ~2,000 tests
tools/                  the checks that keep the structure honest
docs/                   rules, specs, architecture, history
```

Layers point downward only:
`frontend → controllers → services → db`. The frontend never touches the
database directly; every DB call is dispatched off the UI event loop by a
controller. This is enforced, not conventional — see
[docs/system/rules/30-architecture.md](docs/system/rules/30-architecture.md).

## Developing

```bash
pytest tests/ -q                # full suite, ~5 minutes
python -m tools.checks all      # suite + every gate + boot smoke — run before committing
python -m tools.checks gates    # structural gates only, ~10 seconds
```

Four structural gates run on every test run and only ever tighten:

| Gate | Enforces |
|---|---|
| structure | file size, no SQL outside the data layer, no UI database access, declared transactions |
| import contracts | the layering rules, by name |
| facade audit | `TradingRuntime` only shrinks; its public surface is allowlisted |
| orphan detector | no extracted code that nothing calls |

Plus a per-area coverage ratchet: coverage may rise, never fall, and the
money-critical areas carry hand-set floors.

## The rules

Everything an agent or a new contributor needs is in [docs/](docs/).
[docs/system/](docs/system/) is the knowledge base and single point of truth:
[vision/](docs/system/vision/) says why the system exists,
[rules/](docs/system/rules/) what must never be violated, and
[domains/](docs/system/domains/) holds a living file per part of the system.

| | |
|---|---|
| [docs/system/vision/000-goal.md](docs/system/vision/000-goal.md) | what this system is and what it is for |
| **[docs/system/rules/10-golden-rules.md](docs/system/rules/10-golden-rules.md)** | **read this first** |
| [docs/system/rules/20-trading-safety.md](docs/system/rules/20-trading-safety.md) | what can cost money |
| [docs/system/rules/30-architecture.md](docs/system/rules/30-architecture.md) | layers and boundaries |
| [docs/system/rules/40-testing.md](docs/system/rules/40-testing.md) | the testing protocol |
| [docs/system/rules/50-workflow.md](docs/system/rules/50-workflow.md) | how a change gets made |
| [docs/todo/](docs/todo/) | what we are building and why — the plan packs and their SPEC.md files |

The short version: **never** place a real or demo order to test something,
**never** edit a test to make a change pass, write the test first and watch it
fail, and run all the checks before committing.

## Status

Version `0.8.2` — see [CHANGELOG.md](CHANGELOG.md).

Suite: ~2,000 tests, 0 failing. Coverage is high on the trading logic
(`trading` 88%, `risk` 87%, `positions` 86%, `signals` 84%, `db` 92%) and low
on the UI pages by design — those are covered by import and boot tests instead.

Known gaps and open decisions are tracked in
[docs/todo/refactor/stage0/OPEN_QUESTIONS.md](docs/todo/refactor/stage0/OPEN_QUESTIONS.md).
The largest is that `backend/src/controllers/remote/` — licence-token issuance
and admin authority — has no tests yet.

XAUUSD (Gold) trading app with a [NiceGUI](https://nicegui.io) web frontend, a MetaTrader 5
bridge, and Telegram-based signal copying — built to run either as a local desktop app or
unattended on a VPS.

> **This is a live trading system, not a demo.** Enabling the MT5 bridge and Market Order
> controls places real orders against a real broker account. Nothing here is financial advice —
> use at your own risk, and confirm your risk settings before connecting a live account.

## What it does

- Runs one or more signal engines (breakout/ORB, Telegram-copy, reversal) that generate and
  optionally auto-execute trades against MetaTrader 5.
- Connects to MT5 natively on Windows, or through Wine on macOS (see `Start MT5 Bridge.command`
  / `setup_wine_bridge.sh`).
- Reads Telegram channels for third-party signals and can mirror, score, or auto-manage them.
- A NiceGUI web UI (Trading, History, Settings, Reversal Panel, etc.) served locally at
  `http://localhost:8888`.
- A remote client/admin fleet system (`forex_trader/remote/`) so multiple deployed instances
  (VPS, test machines, customer installs) can be monitored, diagnosed, and updated from one
  admin console.

## Requirements

- Python 3.11+
- A MetaTrader 5 terminal with an active broker account (demo or live)
- Windows: the `MetaTrader5` Python package (installed automatically, Windows only)
- macOS: Wine-based MT5 bridge, plus `libomp` and `git` (auto-installed via Homebrew on first
  run if missing — see `FOREX Start.command`)

## Getting started

**Windows** — double-click `Setup & Start FOREX.bat`, or run `FOREX_Trader_Setup.exe` for a
guided install. Either sets up a Python virtual environment, installs dependencies, and starts
the app.

**macOS** — double-click `FOREX Start.command`. First run creates a virtual environment,
installs dependencies, and installs `libomp`/`git` via Homebrew if available. If macOS refuses
to open the file, see
[macOS: "was blocked to protect your Mac"](#macos-was-blocked-to-protect-your-mac) below.

On first launch, a default `config.yaml` is created from `config.yaml.example` in your user data
directory (`%APPDATA%\ForexTrader` on Windows, `~/Library/Application Support/ForexTrader` on
macOS) — enter your MT5/broker and Telegram credentials from the app's Settings page.

To stop the app, use `Stop FOREX.bat` / `FOREX Stop.command`, or the in-app Power dialog.

### macOS: "was blocked to protect your Mac"

The first time you double-click `FOREX Start.command`, macOS may refuse to run it:

> "FOREX Start.command" was blocked to protect your Mac.
> Apple could not verify "FOREX Start.command" is free of malware that may harm your Mac or
> compromise your privacy.

This is Gatekeeper, not a fault with the download. The app is not signed with a paid Apple
Developer ID, and your browser tags every downloaded file with a quarantine flag, so macOS has
no signature to check. macOS applies the block to each `.command` file separately, but you only
have to deal with `FOREX Start.command`: once it runs, it clears the flag from the other
launchers in the folder (`FOREX Stop`, `Start MT5 Bridge`, `Mac Uninstall`) for you.

**Best: never get the flag in the first place.** The quarantine flag is applied by the app that
downloads the files. `git`, `curl` and `tar` do not apply it, so installing from a clone leaves
nothing to unblock, and keeps the app on the git checkout that Settings > Update needs anyway:

```bash
git clone <repo-url> ~/FOREX && open ~/FOREX
```

If you were sent a `.zip`, note that unpacking it in Finder marks every file inside it. A
`.tar.gz` unpacked with `tar -xzf` in Terminal does not.

**Already downloaded through a browser?** Clear the flag for the whole folder in one go. Open
Terminal, type `xattr -dr com.apple.quarantine ` (with the trailing space), drag the FOREX
folder onto the Terminal window to fill in its path, then press Return:

```bash
xattr -dr com.apple.quarantine ~/Downloads/FOREX
```

**Prefer not to use Terminal?** Approve the file through System Settings instead:

1. Double-click `FOREX Start.command` and dismiss the warning.
2. Open System Settings > Privacy & Security and scroll down to the Security section.
3. Next to "FOREX Start.command" was blocked, click **Open Anyway**, then authenticate with
   Touch ID or your password and confirm.

You only need to do this for `FOREX Start.command`. On a successful start it clears the flag
from the other launchers in the folder, so Stop, the MT5 bridge and the uninstaller open
normally from then on.

On macOS 15 (Sequoia) and later, Ctrl-clicking the file and choosing Open no longer bypasses
this. System Settings is the only route without Terminal.

## Uninstalling

Double-click `Windows Uninstall.bat` or `Mac Uninstall.command`. Both remove the app folder,
the user data directory, the licence activation, and any Desktop shortcut, after two
confirmations. On Windows, an install made with `FOREX_Trader_Setup.exe` should instead be
removed via Settings > Apps, which also clears its registry entries and Start Menu shortcuts.

## Project layout

```
forex_trader/       Core app: engines, database, UI pages, remote client/server
installer/          Inno Setup installer source (installer/BUILD_INSTALLER.md has build steps)
mql5/                MetaTrader 5 EA/indicator source
tests/               Test suite
docs/                Planning notes
run.py               App entry point
```

## Updating

Deployed instances can self-update from GitHub (Settings > Update page) once running as a git
checkout, or be pushed an update directly from the admin console — see
`forex_trader/core/core_app_update.py` and `forex_trader/remote/`.

## License

Private project — no open-source license is granted. Source is public for deployment/update
tooling purposes only.
