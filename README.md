# FOREX Trader

An automated XAUUSD (gold) trading application. It runs its own signal engines,
reads signals from Telegram channels, applies risk rules, places and manages
real orders through MetaTrader 5, and shows everything in a local web
dashboard.

> **This trades real money on a live broker account.**
> Enabling the MT5 bridge and the order controls places real orders against a
> real account. Nothing here is financial advice — use at your own risk, and
> confirm your risk settings before connecting a live account.
> If you are an AI agent, read [CLAUDE.md](CLAUDE.md) before changing anything.

![The chart tab](docs/images/dashboard-chart.png)

---

## Running it

```bash
pip install -r requirements.txt
python run.py
```

The dashboard opens at **http://localhost:8888**.

You will need a licence key on first run, and MT5 credentials configured under
**Settings → MT5 / Bridge**.

| Script | What it does |
|---|---|
| `python run.py` | start the app |
| `Setup & Start FOREX.bat` | Windows: install deps and start |
| `FOREX_Trader_Setup.exe` | Windows: guided install |
| `FOREX Start.command` | macOS: start |
| `Start MT5 Bridge.command` | macOS: start the MT5 bridge alone |
| `Stop FOREX.bat` / `FOREX Stop.command` | stop the app |

**Requirements**

- Python 3.11+
- A MetaTrader 5 terminal with a broker account (demo or live)
- Windows: the `MetaTrader5` Python package (installed automatically, Windows only)
- macOS: the Wine-based MT5 bridge, plus `libomp` and `git` (auto-installed via
  Homebrew on first run if missing — see `FOREX Start.command` and
  `setup_wine_bridge.sh`)

On first launch a default `config.yaml` is created from `config.yaml.example` in
your user data directory (`%APPDATA%\ForexTrader` on Windows,
`~/Library/Application Support/ForexTrader` on macOS). Nothing in it needs
editing by hand — every credential is entered through the Settings screens.

If macOS refuses to open the launcher, see
[macOS: "was blocked to protect your Mac"](#macos-was-blocked-to-protect-your-mac).

## What it does

**Signals in.** Runs its own engines (breakout/ORB and reversal) and watches
configured Telegram channels, parsing entry/SL/TP out of messages (several
channel formats, plus an AI fallback for format drift) and deduplicating
reposts and edits.

**Decisions.** A signal must survive staleness checks, logic-keyword filters, a
minimum reward:risk floor, a cap on correlated open trades, session and
schedule gates, and — when enabled — a deterministic Risk Governor that sizes
every position from risk percentage and the real stop distance.

**Orders out.** Places market or pending orders via the MT5 bridge, then
manages each trade to its strategy: scale-out ladders, breakeven runners,
trailing stops, ORB/IVB fixed setups, basket harvesting, and more.
Take-profit ladders are polled sub-second because gold levels can sit a point
apart.

![The trading tab](docs/images/dashboard-trading.png)

**Watching.** Reconciles the app's view against what the broker actually holds,
recovers from bridge outages, syncs realised profit, runs a circuit breaker
that pauses new trades after a run of losses, and alerts to Telegram.

**Extras.** Backtesting every strategy template side by side against historical
candles or your own closed trades, an ORB/IVB report, AI trade commentary,
email reports, and a remote client/admin fleet so several deployed instances
(VPS, test machines, customer installs) can be monitored and updated from one
admin console.

![The backtest tab](docs/images/dashboard-backtest.png)

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
    api/                the HTTP layer the dashboard talks to — routers only
    utils/              bottom of the stack
backend/migrations/     numbered, tested schema upgrade steps

frontend/               React dashboard (TypeScript, Vite)
    src/                components, hooks, the one HTTP client
    dist/               the compiled bundle — committed, and what the app serves
mql5/                   MetaTrader 5 EA and indicator source
installer/              Inno Setup installer source (see installer/BUILD_INSTALLER.md)
notebooks/              research notebooks
tests/                  ~7,700 tests
tools/                  the checks that keep the structure honest
docs/                   rules, specs, architecture, history
```

Layers point downward only:
`frontend (browser) → backend/src/api → controllers → services → db`. The
dashboard runs in the browser and reaches the app over HTTP/JSON; the API layer
never touches the database or a service directly. This is enforced, not
conventional — see
[docs/system/rules/30-architecture.md](docs/system/rules/30-architecture.md).

**Node is a developer dependency only.** The dashboard is compiled ahead of
time and `frontend/dist` is committed, so installing and running the app needs
nothing but Python.

## Developing

```bash
pytest tests/ -q                # full suite
python -m tools.checks all      # suite + every gate + boot smoke — run before committing
python -m tools.checks gates    # structural gates only, ~12 seconds
```

The dashboard has its own toolchain:

```bash
cd frontend
npm install                     # once per checkout
npm test                        # vitest
npm run dev                     # Vite on :5173, proxying /api to the app on :8888
npm run build                   # rebuild dist/ — commit it with your src change
```

`tools.checks gates` runs nine checks, and they only ever tighten:

| Check | Enforces |
|---|---|
| structure gates | file size, no SQL outside the data layer, no UI database access, declared transactions |
| import contracts | the layering rules, by name |
| runtime facade | `TradingRuntime` only shrinks; its public surface is allowlisted |
| orphan modules | no extracted code that nothing calls |
| undefined names | nothing left dangling by a file split |
| unawaited coroutines | no coroutine created and dropped |
| late binding | no import-time capture of a value that changes |
| boot smoke | the app still starts |
| doc links | every relative Markdown link resolves |

Plus a per-area coverage ratchet: coverage may rise, never fall, and the
money-critical areas carry hand-set floors.

## Settings

Every credential and tunable is entered in the app, under **Settings** — MT5 and
the EA bridge, Telegram, AI, email reports, the remote node, security,
registration, updates and the expert tunables.

![The settings tab](docs/images/dashboard-settings.png)

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

The short version: **never** place a real trade on a live account (demo is
fine, the suite is not),
**never** edit a test to make a change pass, write the test first and watch it
fail, and run all the checks before committing.

## Status

Version `0.5` — see [CHANGELOG.md](CHANGELOG.md).

Suite: ~7,700 tests. Coverage is high on the trading logic (`signals` 90%,
`trading` 90%, `risk` 89%, `positions` 88%, `db` 95%) and low on the UI pages by
design — those are covered by import and boot tests instead. The per-area
floors live in
[tools/refactor_audit/coverage_baseline.json](tools/refactor_audit/coverage_baseline.json).

Open decisions and known gaps are tracked in
[docs/simon-handover/](docs/simon-handover/) and [docs/todo/](docs/todo/).

## Updating

Deployed instances can self-update from GitHub (**Settings → Update**) once
running as a git checkout, or be pushed an update directly from the admin
console — see `backend/src/services/positions/core_app_update.py` and
`backend/src/services/cluster/`.

## macOS: "was blocked to protect your Mac"

The first time you double-click `FOREX Start.command`, macOS may refuse to run
it:

> "FOREX Start.command" was blocked to protect your Mac.
> Apple could not verify "FOREX Start.command" is free of malware that may harm your Mac or
> compromise your privacy.

This is Gatekeeper, not a fault with the download. The app is not signed with a
paid Apple Developer ID, and your browser tags every downloaded file with a
quarantine flag, so macOS has no signature to check. macOS applies the block to
each `.command` file separately, but you only have to deal with
`FOREX Start.command`: once it runs, it clears the flag from the other
launchers in the folder (`FOREX Stop`, `Start MT5 Bridge`, `Mac Uninstall`) for
you.

**Best: never get the flag in the first place.** The quarantine flag is applied
by the app that downloads the files. `git`, `curl` and `tar` do not apply it, so
installing from a clone leaves nothing to unblock, and keeps the app on the git
checkout that Settings → Update needs anyway:

```bash
git clone <repo-url> ~/FOREX && open ~/FOREX
```

If you were sent a `.zip`, note that unpacking it in Finder marks every file
inside it. A `.tar.gz` unpacked with `tar -xzf` in Terminal does not.

**Already downloaded through a browser?** Clear the flag for the whole folder in
one go. Open Terminal, type `xattr -dr com.apple.quarantine ` (with the trailing
space), drag the FOREX folder onto the Terminal window to fill in its path, then
press Return:

```bash
xattr -dr com.apple.quarantine ~/Downloads/FOREX
```

**Prefer not to use Terminal?** Approve the file through System Settings
instead:

1. Double-click `FOREX Start.command` and dismiss the warning.
2. Open System Settings → Privacy & Security and scroll down to the Security section.
3. Next to "FOREX Start.command" was blocked, click **Open Anyway**, then authenticate with
   Touch ID or your password and confirm.

On macOS 15 (Sequoia) and later, Ctrl-clicking the file and choosing Open no
longer bypasses this. System Settings is the only route without Terminal.

## Uninstalling

Double-click `Windows Uninstall.bat` or `Mac Uninstall.command`. Both remove the
app folder, the user data directory, the licence activation, and any Desktop
shortcut, after two confirmations. On Windows, an install made with
`FOREX_Trader_Setup.exe` should instead be removed via Settings → Apps, which
also clears its registry entries and Start Menu shortcuts.

## License

Private project — no open-source license is granted. Source is public for
deployment/update tooling purposes only.
