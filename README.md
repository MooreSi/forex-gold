# FOREX Trader

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
installs dependencies, and installs `libomp`/`git` via Homebrew if available.

On first launch, a default `config.yaml` is created from `config.yaml.example` in your user data
directory (`%APPDATA%\ForexTrader` on Windows, `~/Library/Application Support/ForexTrader` on
macOS) — enter your MT5/broker and Telegram credentials from the app's Settings page.

To stop the app, use `Stop FOREX.bat` / `FOREX Stop.command`, or the in-app Power dialog.

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
