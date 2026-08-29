# Installing FOREX Trader from scratch

**Who this is for:** you, on a machine with nothing set up, with nobody else
present. If you get stuck at any step, that step has a "when it goes wrong"
box — those are the failures that have actually happened, not a generic
troubleshooting list.

**What this does not cover:** what to do once the app is running. That is the
**Start Here** checklist inside the app, which walks you through the licence,
connecting MT5, Algo Trading, risk per trade, and Telegram. This page ends the
moment the dashboard opens.

**Time:** about 20 minutes on Windows, most of it waiting for downloads.

---

## Before you begin

You need three things. Get them now; the install stops dead without them.

| Thing | Where it comes from | Needed for |
|---|---|---|
| A licence key | Simon, from the admin server | The app refuses to start without one |
| MT5 account: login, password, server name | Your broker (Vantage) | Placing any order |
| Telegram API id + hash *(optional)* | my.telegram.org → API development tools | Reading signals from channels |

The Telegram pair is genuinely optional — the app runs and trades without it.
The other two are not.

---

## Windows

### 1. Get the folder onto the machine

Copy the whole `Forex-Update` folder wherever you want it to live. Documents is
fine. It does not need to be on C:.

> **Avoid:** a path with accents, `#`, `&` or similar. The virtual environment
> step fails on those, and the error it gives ("Could not create virtual
> environment") does not say why. A plain path avoids it.

### 2. Double-click **`Setup & Start FOREX.bat`**

That one file does everything: finds Python, installs it if missing, creates
the virtual environment, installs the dependencies, and starts the app. First
run takes a few minutes. Later runs take seconds.

It looks for Python 3.11 or newer in several places, and if it finds none it
downloads and installs Python 3.11 for you — **no admin rights needed**.

> **When it goes wrong**
>
> - *"ERROR: Download failed"* — no internet, or a firewall blocked it. Fix the
>   connection and run the file again. It picks up where it left off.
> - *"ERROR: Python installation did not complete"* — install Python 3.11
>   yourself from python.org, tick **Add Python to PATH** during install, then
>   run the .bat again.
> - *"ERROR: Failed to install dependencies"* — almost always internet. Run it
>   again.
> - *Nothing happens at all, or the window flashes and closes* — right-click
>   the file, Properties, and click **Unblock** if that button is there.
>   Windows blocks files copied from another machine.

### 3. The browser opens on the dashboard

If it does not, open `http://localhost:8888` yourself.

> **When it goes wrong**
>
> - *"This site can't be reached"* — the app is still starting. Give it 30
>   seconds and refresh.
> - *Still nothing after a minute* — look at the black command window. The
>   error will be in there. The most common one is a port clash: something
>   else is already using 8888.

### 4. Enter your licence key

The app shows an activation screen before anything else. Paste the key Simon
gave you.

> **When it goes wrong**
>
> - *"This licence was issued for a different machine"* — exactly what it says.
>   The key is tied to the machine it was issued for. Ask Simon for one for
>   this machine; the screen shows the machine id he needs.
> - *"Your saved licence key is no longer valid for this version"* — the key
>   needs reissuing after an upgrade. The same screen requests a new one.
> - *"Your licence expired on ..."* — request a renewal from that screen.
>
> All three land on the same activation screen on purpose: it can request a new
> licence and accept one pushed from the admin server, so you are never stuck
> with no way forward.

### 5. Stopping it

**`Stop FOREX.bat`**. Closing the black window also works but is less tidy.

---

## macOS

MetaTrader 5 has no native Mac build, so MT5 runs under Wine and the app talks
to it through a bridge. This is more involved than Windows and is the reason
Windows is the recommended machine for live trading.

### 1. Prerequisites

- Python 3.11 or newer (`python3 --version`)
- Wine, via CrossOver or Homebrew
- MetaTrader 5 installed inside that Wine prefix, logged in to your account

### 2. One-time bridge setup

```bash
./setup_wine_bridge.sh
```

It finds your Wine binary, migrates an existing CrossOver MT5 bottle (or
creates a fresh prefix), checks Python 3.11 and the MetaTrader5 package are
available inside it, and writes the prefix path into `config.yaml`.

Run it once per machine.

### 3. Start the app

```bash
python run.py
```

Then open `http://localhost:8888`.

> **When it goes wrong**
>
> - *The dashboard loads but MT5 shows disconnected* — start MT5 inside Wine
>   first and log in, then restart the app. The bridge cannot start a terminal
>   that is not running.
> - *Orders are refused with no clear reason* — check the **AutoTrading**
>   (robot) button in the MT5 toolbar is ON. It is MT5's own master switch and
>   overrides everything the app does.

---

## After it starts

Open **Start Here** in the app. It walks the rest: MT5 connection, Algo
Trading, risk per trade, Telegram, and confirming you are in Demo.

**Stay in Demo until you have watched it trade.** Demo means no real money
moves. The Demo/Live switch is in Settings, and there is no reason to touch it
on day one.

---

## Where things live

Knowing this makes most problems diagnosable.

| What | Windows | macOS |
|---|---|---|
| Your data, settings, logs | `%APPDATA%\ForexTrader\` | `~/Library/Application Support/ForexTrader/` |
| The log | `...\data\forex_trader.log` | `.../data/forex_trader.log` |
| Databases | `...\data\forex_trader_demo.db` and `_live.db` | same |
| Licence + certificates | `...\remote\` | `.../remote/` |

Your settings and trade history live in that data folder, **not** in the
program folder. You can delete and re-copy the program folder without losing
anything.

The log is the first place to look for anything unexplained. It is plain text
and the most recent entries are at the bottom.

---

## Two things worth knowing early

**Nothing trades until you switch it on.** Auto-execution is off by default,
so the app will sit and watch until you tell it otherwise.

**The protective limits pause trading; they never close a position.** If a
daily-loss or drawdown limit is hit, the app stops opening *new* trades. It
does not liquidate anything you already hold.
