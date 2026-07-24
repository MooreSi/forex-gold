# Building the Windows Installer

## Prerequisites (on a Windows machine or VM)

1. Install **Inno Setup 6**: https://jrsoftware.org/isinfo.php

That's it — the Python 3.11 embeddable runtime and get-pip.py are no longer bundled at
compile time. `[Code]`'s `FetchPythonEmbed` downloads both fresh during install via
PowerShell's `Invoke-WebRequest`/`Expand-Archive`, which ship with every Windows 10+ target
this installer already requires. No local prerequisite files, no third-party download plugin.

## Build Steps

1. Open `installer/FOREX_Trader_Setup.iss` in Inno Setup Compiler
2. Press **F9** (Build → Compile)
3. Wait for compilation (2-5 minutes on first run)
4. Installer appears at the repo root: `FOREX_Trader_Setup.exe` (per `OutputDir = ..` in the .iss)

## What the Installer Does

For the user (hands-off):
1. Checks if MetaTrader 5 is installed (warns if not)
2. Copies all app files to `%LOCALAPPDATA%\FOREX Trader\`
3. Downloads the Python 3.11 embeddable runtime + get-pip.py (requires internet)
4. Bootstraps pip into the downloaded Python
5. Creates a Python virtual environment at `%LOCALAPPDATA%\FOREX Trader\.venv\`
6. Installs all packages from requirements.txt (~5 minutes, requires internet)
7. Adds Windows Firewall rules for ports 8888 and 9000 (requires the one admin UAC prompt)
8. Creates Start Menu and optional desktop shortcuts
9. Optionally launches the app immediately after install

## User Setup After Installation (MT5 side)

The user still needs to:
1. Open MetaTrader 5 from their broker and log in
2. Enable "Algo Trading" in the MT5 toolbar (robot icon → green)
3. Enter their credentials in FOREX Trader → Settings → MT5/Bridge

These steps cannot be automated as they depend on the user's specific broker account.

## Updating the Version

Change `AppVersion` and `VersionInfoVersion` at the top of the .iss file — always bump both
for every rebuild, even a same-day one. The installer's `[Code]` section skips reinstalling
entirely when the target machine's `installed_version.txt` already matches `AppVersion`, so
reusing an old version number means a new .exe will silently launch whatever stale app is
already installed instead of deploying the new build. The output filename itself
(`FOREX_Trader_Setup.exe`) does not change with version — Inno Setup overwrites it in place.
