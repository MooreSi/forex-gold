# The Windows installer

`FOREX_Trader_Setup.exe` is a **bootstrapper**. It carries no app files, so an
app change never needs a new .exe (owner, 2026-09-25). Recompile only when
`FOREX_Trader_Setup.iss` itself changes, and bump `InstallerVersion` when you do.
Its version is its own; it does not follow the app's `VERSION`.

## What it does on the user's machine

1. Warns if MetaTrader 5 is not installed.
2. Installs the Microsoft Visual C++ Redistributable (x64). LightGBM's DLL needs
   it, and a bare Windows image (a fresh VPS) has none (bugs/066).
3. Downloads portable Git into `%LOCALAPPDATA%\Programs\PortableGit`, the same
   build and folder `Setup & Start FOREX.bat` uses.
4. Checks out `main` from `https://github.com/MooreSi/forex-gold.git` into
   `%LOCALAPPDATA%\FOREX Trader` (`--depth=1`). An older copied install in that
   folder becomes a checkout in place; its venv is kept.
5. Adds firewall rules for 8888 and 9000 (Private profile; needs admin). It does
   not open 8765: Settings > Remote node > "Make this node a VPS" does that.
6. Runs `Setup & Start FOREX.bat`, which installs Python 3.11 if needed, builds
   the venv, installs `requirements.txt` and starts the app. The first start
   opens the dashboard in the browser, even on a VPS.

Any failure in steps 3-4 keeps the wizard on the Ready page with the reason;
nothing is half-installed, and pressing Install again retries.

Running the .exe again on a machine that already has a checkout and a venv just
starts the app. Updates arrive through Settings > Update (a `git pull`), not
through the .exe. To reinstall from scratch, uninstall first: the uninstaller
removes the whole install folder (settings, databases and logs live in
`%APPDATA%\ForexTrader` and are kept).

## Building

On Windows (or under Wine), with Inno Setup 6: open
`installer/FOREX_Trader_Setup.iss`, press **F9**. The .exe appears at the repo
root. Commit it.

`tests/refactor/test_installer_is_a_bootstrapper.py` pins the design: no bundled
files, the same repo and branch as the in-app updater, the same portable Git as
the launcher, and an uninstaller that can only delete its own folder.
