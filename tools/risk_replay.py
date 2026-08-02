"""Risk-policy replay -- what today's *actual* trades would have returned
under a different sizing/risk policy.

Motivation (2026-07-28): the account went from ~$1,309 to $920 on a day
whose signals were fine -- 67% win rate, 45 wins against 22 losses. The
loss did not come from picking bad trades. It came from position sizing:
`strategy_lot_size = 0.1` is a FIXED lot that bypasses suggest_lot_size()
entirely ("fixed lot always wins" -- see core_fees_sizing.py), so risk per
trade is whatever the signal's stop distance happens to imply. Measured
across 26 stopped-out trades that day, realised risk ranged from $0 to
$149.40 -- 0% to 16.2% of the account -- against a configured intent of
0.5% ($4.60). A single trade could cost 22x what the settings said.

That makes strategy selection almost irrelevant by comparison: a strategy
with a genuine edge still loses if one trade risks 20x another. This tool
exists to measure sizing/risk policies against real closed trades before
any of them go live, the same way breakout_signal/backtest.py gates entry
-logic changes.

Method: each historical position is reduced to an R-multiple --
realised P&L divided by the dollar risk that position carried AT OPEN
(original entry-to-stop distance x volume x contract size). R is
sizing-invariant, so a policy can be replayed by re-sizing each trade and
paying out R x new_risk. This deliberately assumes the same entries,
exits and stop levels -- it answers "what would different SIZING have
done", not "what would different SIGNALS have done", and must not be read
as the latter.

Getting the denominator right matters more than anything else here, and
the obvious choice is wrong: vantage_simulated_trades.stop_loss holds the
FINAL stop, after breakeven and trailing moves. Using it inflates R
without limit -- ticket 1644451197 closed +$31.94 with its trailed stop
0.61 above a BUY entry, which scores as +5R against a $6 "risk" the trade
never actually carried. An earlier draft of this tool did exactly that
and reported a fantasy +$4,641 over 7 days.

So original risk is resolved per trade, in order:
  1. sl_moved_to_be = 0  -> |entry - stop_loss| is genuine; the stop never
     moved, so what it died on is what it opened with.
  2. sl_moved_to_be = 1  -> the opening stop is not stored anywhere. Fall
     back to the median opening distance of that strategy's own untouched
     (case-1) trades, since each strategy sets stops by a fixed rule
     (Conservative's flat 5.00 is visible in the data). This is an
     ESTIMATE. Trades resolved this way are counted and reported
     separately, and are mostly winners -- so their R is the least
     trustworthy part of any result below.
  3. no strategy baseline available -> skipped entirely.

Known limitation: trades whose stop distance can't be recovered at all
are skipped, so counts are lower than the raw closed-trade count.
Skipped trades are reported, not hidden.

Usage:
    .venv/bin/python tools/risk_replay.py                  # last 3 days
    .venv/bin/python tools/risk_replay.py --days 7
    .venv/bin/python tools/risk_replay.py --balance 920
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import sqlite3
import sys
import urllib.request

CONTRACT_SIZE = 100          # XAUUSD: 1.00 lot = 100 oz
# No genuine XAUUSD stop on this system is anywhere near this wide (the
# widest per-strategy median is ~10). A larger "distance" means a corrupt
# row -- typically an EA-grid placeholder left at entry_price = 0.0, whose
# stop distance then reads as the entire gold price and produces a
# five-figure phantom risk. Guarded rather than silently averaged in.
MAX_PLAUSIBLE_STOP_DISTANCE = 100.0
BROKER_UTC_OFFSET = 3 * 3600  # Vantage MT5 stamps deals in UTC+3, not UTC
BRIDGE = "http://localhost:9010"
DB = ("/Users/simon/Library/Application Support/ForexTrader/"
      "data/forex_trader_demo.db")


def _fetch_history(days: int) -> list[dict]:
    with urllib.request.urlopen(f"{BRIDGE}/history?days={days}", timeout=30) as r:
        return json.load(r)["history"]


def _db_trades() -> dict[int, sqlite3.Row]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return {
        r["mt5_ticket"]: r
        for r in con.execute(
            "SELECT mt5_ticket, entry_price, stop_loss, direction, strategy, "
            "tg_source, sl_moved_to_be FROM vantage_simulated_trades "
            "WHERE status='closed' AND mt5_ticket > 0"
        )
    }


def _strategy_stop_baselines(rows: dict[int, sqlite3.Row]) -> dict[str, float]:
    """Median opening stop distance per strategy, measured only on trades
    whose stop demonstrably never moved (sl_moved_to_be = 0). Used to
    reconstruct the opening risk of trades that DID move to breakeven,
    whose original stop the schema doesn't retain."""
    per: dict[str, list[float]] = collections.defaultdict(list)
    for r in rows.values():
        if r["sl_moved_to_be"]:
            continue
        entry, sl = r["entry_price"] or 0, r["stop_loss"] or 0
        if entry and sl:
            d = abs(entry - sl)
            if 0.01 < d <= MAX_PLAUSIBLE_STOP_DISTANCE:
                per[r["strategy"]].append(d)
    out = {}
    for strategy, ds in per.items():
        ds.sort()
        out[strategy] = ds[len(ds) // 2]
    return out


class Trade:
    """One closed position, reduced to a sizing-invariant R-multiple."""

    __slots__ = ("day", "ts", "r", "risk", "pnl", "strategy", "source", "risk_pct_of")

    def __init__(self, day, ts, r, risk, pnl, strategy, source):
        self.day, self.ts, self.r = day, ts, r
        self.risk, self.pnl = risk, pnl
        self.strategy, self.source = strategy, source


def build_trades(days: int) -> tuple[list[Trade], int, int]:
    """Returns (trades, skipped, n_estimated) -- n_estimated counts trades
    whose opening risk came from the per-strategy median rather than from
    an untouched stop."""
    hist = _fetch_history(days)
    rows = _db_trades()
    baselines = _strategy_stop_baselines(rows)

    positions: dict[int, dict] = collections.defaultdict(
        lambda: {"in": None, "outs": []}
    )
    for d in hist:
        if d.get("entry") == 0:
            positions[d["position_id"]]["in"] = d
        elif d.get("entry") == 1:
            positions[d["position_id"]]["outs"].append(d)

    trades, skipped, estimated = [], 0, 0
    for pid, v in positions.items():
        if not v["in"] or not v["outs"] or pid not in rows:
            skipped += 1
            continue
        row, ind = rows[pid], v["in"]
        entry = ind["price"]
        if not entry:
            skipped += 1
            continue

        if row["sl_moved_to_be"]:
            # Opening stop not retained -- fall back to this strategy's
            # own typical opening distance. Estimated, and flagged as such.
            stop_distance = baselines.get(row["strategy"], 0.0)
            if stop_distance <= 0.01:
                skipped += 1
                continue
            estimated += 1
        else:
            stop_distance = abs(entry - (row["stop_loss"] or 0))
            if stop_distance <= 0.01 or stop_distance > MAX_PLAUSIBLE_STOP_DISTANCE:
                skipped += 1
                continue

        risk = stop_distance * ind["volume"] * CONTRACT_SIZE
        if risk <= 0:
            skipped += 1
            continue
        pnl = sum(o["profit"] + o.get("swap", 0) + o.get("fee", 0) for o in v["outs"])
        ts = ind["time"] - BROKER_UTC_OFFSET
        trades.append(Trade(
            datetime.datetime.fromtimestamp(ts, datetime.UTC).strftime("%Y-%m-%d"),
            ts, pnl / risk, risk, pnl, row["strategy"], row["tg_source"],
        ))
    trades.sort(key=lambda t: t.ts)
    return trades, skipped, estimated


# ── policies ────────────────────────────────────────────────────────────
# Each returns the dollar risk to assign a trade, given running state.
# `None` means "don't take this trade" (circuit breaker tripped).

def policy_actual(t: Trade, state: dict) -> float | None:
    """What actually happened -- fixed 0.1 lot, risk set by stop distance."""
    return t.risk


def make_fixed_fractional(risk_pct: float, daily_stop_pct: float | None = None):
    """Risk a constant % of *current* balance per trade, optionally halting
    for the rest of the day once cumulative loss exceeds daily_stop_pct.

    This is what suggest_lot_size() already computes -- the live system
    simply never calls it while strategy_lot_size is non-zero.
    """
    def policy(t: Trade, state: dict) -> float | None:
        if daily_stop_pct is not None:
            if state["day"] != t.day:
                state["day"], state["day_start"] = t.day, state["balance"]
            drawdown = state["day_start"] - state["balance"]
            if drawdown >= state["day_start"] * daily_stop_pct / 100:
                return None
        return state["balance"] * risk_pct / 100
    return policy


def replay(trades: list[Trade], policy, start_balance: float) -> dict:
    state = {"balance": start_balance, "day": None, "day_start": start_balance}
    by_day: dict[str, float] = collections.defaultdict(float)
    skipped_by_breaker = 0
    peak = start_balance
    max_dd = 0.0

    for t in trades:
        if state["day"] is None:
            state["day"], state["day_start"] = t.day, state["balance"]
        risk = policy(t, state)
        if risk is None:
            skipped_by_breaker += 1
            continue
        pnl = t.r * risk
        state["balance"] += pnl
        by_day[t.day] += pnl
        peak = max(peak, state["balance"])
        max_dd = max(max_dd, peak - state["balance"])

    return {
        "final": state["balance"],
        "by_day": dict(by_day),
        "skipped": skipped_by_breaker,
        "max_dd": max_dd,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--balance", type=float, default=None,
                    help="starting balance (default: live account balance)")
    args = ap.parse_args()

    trades, skipped, estimated = build_trades(args.days)
    if not trades:
        print("No usable trades (need both MT5 deal history and a DB stop_loss).")
        return 1

    if args.balance is None:
        with urllib.request.urlopen(f"{BRIDGE}/account", timeout=15) as r:
            args.balance = json.load(r)["balance"]

    days = sorted({t.day for t in trades})
    print(f"{len(trades)} replayable trades over {len(days)} days "
          f"({days[0]} .. {days[-1]}); {skipped} skipped (no recoverable stop)")
    print(f"starting balance ${args.balance:,.2f}")
    if estimated:
        print(f"NOTE: {estimated}/{len(trades)} ({100*estimated/len(trades):.0f}%) "
              f"had their stop moved to breakeven, so their opening risk is "
              f"ESTIMATED from the per-strategy median. These are mostly "
              f"winners -- treat upside figures with caution.")
    print()

    print("=== risk actually taken per trade ===")
    risks = sorted(t.risk for t in trades)
    print(f"  min ${risks[0]:.2f}  median ${risks[len(risks)//2]:.2f}  "
          f"max ${risks[-1]:.2f}   ({100*risks[-1]/args.balance:.1f}% of balance "
          f"on the largest)\n")

    policies = [
        ("actual (fixed 0.1 lot)", policy_actual),
        ("0.5% fixed-fractional", make_fixed_fractional(0.5)),
        ("1.0% fixed-fractional", make_fixed_fractional(1.0)),
        ("0.5% + 3% daily stop", make_fixed_fractional(0.5, daily_stop_pct=3.0)),
        ("1.0% + 3% daily stop", make_fixed_fractional(1.0, daily_stop_pct=3.0)),
    ]

    width = max(len(n) for n, _ in policies)
    header = f"{'policy':{width}} {'final':>10} {'P/L':>10} {'maxDD':>8} {'skip':>5}  "
    header += "  ".join(f"{d[5:]:>8}" for d in days)
    print(header)
    print("-" * len(header))
    for name, pol in policies:
        res = replay(trades, pol, args.balance)
        pnl = res["final"] - args.balance
        line = (f"{name:{width}} {res['final']:10,.2f} {pnl:+10.2f} "
                f"{res['max_dd']:8.2f} {res['skipped']:5d}  ")
        line += "  ".join(f"{res['by_day'].get(d, 0.0):+8.2f}" for d in days)
        print(line)

    print("\n=== edge by strategy (R-multiples, sizing-invariant) ===")
    by_strat = collections.defaultdict(list)
    for t in trades:
        by_strat[t.strategy].append(t.r)
    print(f"  {'strategy':24} {'n':>4} {'sumR':>8} {'avgR':>8}")
    for s, rs in sorted(by_strat.items(), key=lambda kv: -sum(kv[1])):
        flag = "" if len(rs) >= 20 else "   (small sample)"
        print(f"  {s:24} {len(rs):4d} {sum(rs):8.2f} {sum(rs)/len(rs):+8.3f}{flag}")

    print("\nR-multiples assume identical entries, exits and stops -- this "
          "isolates SIZING, not signal quality.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
