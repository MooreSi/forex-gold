# 000 — What this system is and what it is for

**Status:** Living document
**Touches money:** yes — the whole system does
**Created:** 2026-08-08

This is the plain-English answer to "what is this thing?" — the context every
spec, rule and domain file in `docs/system/` assumes. If anything else
conflicts with this document, this document wins or must be updated.

---

## What it is

FOREX Trader is an automated gold-trading application. It trades **XAUUSD**
(gold against the US dollar) with **real money** on a live MetaTrader 5
broker account, and shows everything it does on a local web dashboard at
`http://localhost:8888`.

It is a personal/family trading tool, not a product for the public. One or
two people run it, watch it, and tune it.

## What it does, in one paragraph

It watches Telegram channels where humans post trade signals ("buy gold at
this price, stop-loss here, take-profits there"), parses those messages into
structured trades, decides whether each trade is worth taking and how big it
should be, places the order through MetaTrader 5, and then babysits the open
trade — taking partial profits at each target level, moving stops to
breakeven, trailing winners — until it is closed. Alongside the Telegram
signals it also runs its own research engines (breakout and reversal) that
can generate and execute trades without any human signal at all.

## The goal

**Make a steady, capped daily profit without a human watching the screen.**

The current operating target being tested (August 2026): the system trades
each day and **stops opening new trades once it has made £300 that day**.
Not "make as much as possible" — make the target, then stand down until the
next trading window. The bet is that a modest, repeatable daily take with
strict risk control beats letting it run unbounded and giving profits back.

This is implemented by the **Trading Schedule** gate
(`backend/src/services/risk/schedule.py`): each day has up to three
time-of-day windows, each with its own profit target. Once the sum of closed
profit inside a window reaches its target, all new automated entries are
blocked until the next window opens. Manual orders and signal *ingestion*
are never blocked — only the final automated "place an order" step.

## How it makes decisions

A signal does not become a trade automatically. It must pass, in order:

1. **Freshness** — stale or edited-repost signals are dropped.
2. **Content filters** — keyword and logic filters per channel.
3. **Reward:risk floor** — the trade must offer enough reward for its risk.
4. **Correlation cap** — not too many similar trades open at once.
5. **Session and schedule gates** — is trading allowed right now, and has
   today's profit target already been hit?
6. **Risk Governor** (when enabled) — position size is computed from a risk
   percentage and the real stop distance, deterministically. No gut-feel
   sizing.

Only then is an order placed.

## What "safe" means here

Because this places real orders with real money, the repo is unusually
strict:

- Nothing may ever place, close or modify a real or demo order as a test.
- The close path (the code that exits trades) is frozen — moved verbatim
  only, never reshaped without owner sign-off.
- ~2,000 tests plus structural gates run before every commit.

See `docs/system/rules/10-golden-rules.md` for the full rules.

## What success looks like

- The system runs unattended through its trading windows each day.
- It hits its daily target more days than not, and on losing days the Risk
  Governor keeps the damage small and pre-decided.
- Every trade it takes can be explained afterwards: which signal, which
  gates it passed, why it was sized the way it was, why it closed.

## Non-goals

- Trading instruments other than XAUUSD.
- High-frequency or latency-sensitive trading.
- Serving multiple users or running as a hosted service.
- Maximising profit at the cost of unbounded risk — the daily cap is the
  point, not a limitation to engineer around.
