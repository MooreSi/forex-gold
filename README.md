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

Everything an agent or a new contributor needs is in [docs/](docs/):

| | |
|---|---|
| **[docs/system/rules/10-golden-rules.md](docs/system/rules/10-golden-rules.md)** | **read this first** |
| [docs/system/rules/20-trading-safety.md](docs/system/rules/20-trading-safety.md) | what can cost money |
| [docs/system/rules/30-architecture.md](docs/system/rules/30-architecture.md) | layers and boundaries |
| [docs/system/rules/40-testing.md](docs/system/rules/40-testing.md) | the testing protocol |
| [docs/system/rules/50-workflow.md](docs/system/rules/50-workflow.md) | how a change gets made |
| [docs/specs/](docs/specs/) | what we are building and why |

The short version: **never** place a real or demo order to test something,
**never** edit a test to make a change pass, write the test first and watch it
fail, and run all the checks before committing.

## Status

Version `0.8.2` — see [CHANGELOG.md](CHANGELOG.md).

Suite: ~2,000 tests, 0 failing. Coverage is high on the trading logic
(`trading` 88%, `risk` 87%, `positions` 86%, `signals` 84%, `db` 92%) and low
on the UI pages by design — those are covered by import and boot tests instead.

Known gaps and open decisions are tracked in
[docs/history/refactor-2026/OPEN_QUESTIONS.md](docs/history/refactor-2026/OPEN_QUESTIONS.md).
The largest is that `backend/src/controllers/remote/` — licence-token issuance
and admin authority — has no tests yet.
