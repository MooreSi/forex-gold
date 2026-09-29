"""What the Trend PA panel says about a set of closed trades. Pure.

Everything is in R, the risk each trade took, because that is what the
strategy controls; the dollar balance is a virtual $1,000 risking 1% a trade,
compounded, so the curve shows what compounding the edge (or its absence)
does. The break-even win rate sits beside the win rate: at 1:2 a no-edge
entry wins a third of the time, and a win rate read without that line is the
mistake the engines README records for the Reversal engine.
"""
from __future__ import annotations

from typing import Optional

START_BALANCE = 1000.0
RISK_PCT = 1.0


def _split(rows: list, key: str) -> list:
    groups: dict = {}
    for r in rows:
        groups.setdefault(r.get(key) or "?", []).append(r)
    out = []
    for k, g in sorted(groups.items()):
        wins = sum(1 for r in g if r["outcome"] == "win")
        total = sum(float(r["r_net"]) for r in g)
        out.append({"key": k, "n": len(g), "wins": wins,
                    "win_rate": wins / len(g), "total_r": total,
                    "avg_r": total / len(g)})
    return out


def summarize(rows: list, rr: float, start_balance: float = START_BALANCE,
              risk_pct: float = RISK_PCT) -> dict:
    rows = sorted(rows, key=lambda r: float(r.get("closed_at") or 0))
    n = len(rows)
    rs = [float(r["r_net"]) for r in rows]
    wins = sum(1 for r in rows if r["outcome"] == "win")
    losses = sum(1 for r in rows if r["outcome"] == "loss")
    gain = sum(x for x in rs if x > 0)
    pain = -sum(x for x in rs if x < 0)

    running = peak = max_dd = 0.0
    balance = bal_peak = start_balance
    max_dd_pct = 0.0
    curve = []
    for r, x in zip(rows, rs):
        running += x
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
        balance *= 1 + risk_pct / 100.0 * x
        bal_peak = max(bal_peak, balance)
        max_dd_pct = max(max_dd_pct, (bal_peak - balance) / bal_peak * 100.0)
        curve.append({"ts": r.get("closed_at"), "balance": round(balance, 2),
                      "r": round(running, 3)})

    avg: Optional[float] = sum(rs) / n if n else None
    return {
        "n": n, "wins": wins, "losses": losses, "timeouts": n - wins - losses,
        "win_rate": wins / n if n else None,
        "breakeven_win_rate": 1.0 / (1.0 + rr),
        "total_r": sum(rs), "avg_r": avg, "expectancy_r": avg,
        "profit_factor": gain / pain if pain > 0 else None,
        "max_drawdown_r": max_dd,
        "balance": balance, "start_balance": start_balance,
        "max_drawdown_pct": max_dd_pct,
        "by_session": _split(rows, "session"),
        "by_pattern": _split(rows, "pattern"),
        "by_direction": _split(rows, "direction"),
        "curve": curve[-300:],
    }
