# The system knowledge base

This directory is the **single point of truth** for what this system is,
what it must never do, and what we know about each of its parts. Think of it
as a game design document: the code says *how*, this says *what and why*.

Every file here is a **living file**. When you learn something non-obvious,
settle an open question, or introduce a new constraint, update the relevant
file in the same change that taught you it. Knowledge that stays in a chat
transcript is lost.

## Map

```
system/
  vision/     why this system exists and what success looks like
  rules/      the non-negotiables — safety, architecture, testing, workflow
  domains/    one directory per part of the system — living knowledge
```

### vision/

| File | What it answers |
|---|---|
| [vision/000-goal.md](vision/000-goal.md) | What is this system? What is it for? What is the current operating target? |

### rules/ — read before changing anything

| File | What it covers |
|---|---|
| [rules/00-start-here.md](rules/00-start-here.md) | Orientation for agents |
| [rules/10-golden-rules.md](rules/10-golden-rules.md) | **The golden rules — mandatory** |
| [rules/20-trading-safety.md](rules/20-trading-safety.md) | What can cost money |
| [rules/30-architecture.md](rules/30-architecture.md) | Layers and boundaries, enforced by gates |
| [rules/40-testing.md](rules/40-testing.md) | The testing protocol |
| [rules/50-workflow.md](rules/50-workflow.md) | How a change gets made |
| [rules/60-adding-a-tunable.md](rules/60-adding-a-tunable.md) | Making a constant configurable |
| [rules/70-file-organisation.md](rules/70-file-organisation.md) | Splitting big files |

### domains/ — the parts of the system

Each domain directory holds a `README.md` with the same shape: what it is,
where the code lives, its constraints, known things and gotchas, and open
questions.

| Domain | Covers | Backend packages |
|---|---|---|
| [domains/signals/](domains/signals/) | Telegram ingestion, parsing, signal resolution, bot, alerts | `signals`, `channels`, `telegram` |
| [domains/trading/](domains/trading/) | Order placement, strategy handlers, the close path, DPM | `trading`, `dpm` |
| [domains/risk/](domains/risk/) | Risk Governor, trading schedule, session gates, fees | `risk` |
| [domains/positions/](domains/positions/) | Trade monitoring, TP tracking, broker reconciliation | `positions` |
| [domains/broker/](domains/broker/) | MT5 bridge, EA bridge, watchdog, recovery | `broker`, `health`, `mt5_bridge.py`, `mql5/` |
| [domains/engines/](domains/engines/) | Breakout, reversal, test-signal engines; backtesting | `breakout_signal`, `reversal_engine`, `test_signal`, `backtest` |
| [domains/analytics/](domains/analytics/) | Reports, AI commentary, notifications | `analytics`, `ai`, `notifications` |
| [domains/data/](domains/data/) | Schema, connections, transactions, app_config | `db` |
| [domains/frontend/](domains/frontend/) | The NiceGUI dashboard and its conventions | `frontend/` |
| [domains/platform/](domains/platform/) | Config, licensing, controllers, cluster, install/run | `config`, `cluster`, `controllers`, `run.py`, `installer/` |

## How to use this when building

1. **Before designing** a change, read the affected domain's README —
   constraints and gotchas there override any assumption you brought.
2. **Before writing code**, read [rules/](rules/) if you have not this
   session, and the spec template in [../specs/TEMPLATE.md](../specs/TEMPLATE.md).
3. **After shipping**, fold what the change taught us back into the domain
   README: new constraints, resolved questions, new gotchas.

If a domain file and the code disagree, the code is the fact — fix the file,
and say so in the commit.
