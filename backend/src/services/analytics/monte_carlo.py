"""Monte Carlo on the account's own closed trades (docs/todo/024).

Answers "with these real trades at this size, how bad could the next N
trades get?". The window's net P&L per trade ($, fees in, as the Analysis
table reports it) is resampled with replacement into `PATHS` possible
sequences of the next `horizon` trades, each starting from the broker
balance now, and the spread of outcomes is reported.

Two assumptions, both shown on screen beside the numbers:

  * **Sizing stays as it was.** Each draw is a past trade's $ result, so an
    account that has grown since those trades is simulated at the old size.
  * **Trades are independent.** Losing trades that cluster (one bad regime,
    one bad week) are drawn apart, so streaks are understated and the
    drawdown figures are a floor, not a ceiling.

Seeded: the same trades and balance give the same answer, so a number that
moves between two refreshes moved because the trades did. Read-only.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import numpy as np

from backend.src.services.analytics import trade_table as _trade_table

__all__ = ["for_window"]

PATHS = 5_000
# Below this the resampled paths are mostly the same few trades in a
# different order, and the tails mean nothing.
MIN_TRADES = 30
MAX_HORIZON = 1_000
BAND_POINTS = 50
DD_THRESHOLDS = (10, 20, 30, 50)
_SEED = 24


def max_drawdown_pct(balance: float, pnls: list[float]) -> float:
    """Worst peak-to-trough fall of one path, in % of the peak, capped at 100."""
    eq = balance + np.cumsum(np.asarray(pnls, dtype=float))
    peak = np.maximum.accumulate(np.concatenate(([balance], eq)))[1:]
    dd = np.clip((peak - eq) / peak, 0.0, 1.0)
    return round(float(dd.max()) * 100.0, 2) if len(dd) else 0.0


def _pct(a: np.ndarray, q: float) -> float:
    return round(float(np.percentile(a, q)), 2)


def simulate(pnls: list[float], balance: float, horizon: int) -> dict:
    """The distribution of the next `horizon` trades, or a `note` saying why not."""
    trades = [float(p) for p in pnls if p is not None]
    horizon = max(1, min(int(horizon or 0), MAX_HORIZON))
    base = {"trades": len(trades), "horizon": horizon, "balance": float(balance),
            "paths": PATHS}
    if len(trades) < MIN_TRADES:
        return {**base, "note": f"Needs at least {MIN_TRADES} closed trades in the "
                                f"window; it has {len(trades)}."}
    if not balance or balance <= 0:
        return {**base, "note": "No account balance to start from."}

    rng = np.random.default_rng(_SEED)
    draws = np.asarray(trades)[rng.integers(0, len(trades), size=(PATHS, horizon))]
    eq = balance + np.cumsum(draws, axis=1)
    # Once an account is gone it stays gone: no later winner revives it.
    ruined = np.maximum.accumulate(eq <= 0, axis=1)
    eq = np.where(ruined, 0.0, eq)
    peak = np.maximum.accumulate(np.concatenate((np.full((PATHS, 1), balance), eq), axis=1),
                                 axis=1)[:, 1:]
    dd = np.clip((peak - eq) / peak, 0.0, 1.0).max(axis=1) * 100.0
    final = eq[:, -1]

    step = max(1, horizon // BAND_POINTS)
    cols = sorted(set(range(step - 1, horizon, step)) | {horizon - 1})
    band = [{"n": 0, "p5": float(balance), "p50": float(balance), "p95": float(balance)}]
    band += [{"n": c + 1, "p5": _pct(eq[:, c], 5), "p50": _pct(eq[:, c], 50),
              "p95": _pct(eq[:, c], 95)} for c in cols]

    return {
        **base,
        "mean_trade": round(float(np.mean(trades)), 2),
        "final": {f"p{q}": _pct(final, q) for q in (5, 25, 50, 75, 95)},
        "max_dd_pct": {f"p{q}": _pct(dd, q) for q in (50, 95, 99)},
        "prob_dd": {str(t): round(float(np.mean(dd >= t)), 4) for t in DD_THRESHOLDS},
        "prob_loss": round(float(np.mean(final < balance)), 4),
        "prob_ruin": round(float(np.mean(ruined[:, -1])), 4),
        "band": band,
    }


async def for_window(engine: Any, days: int, horizon: Optional[int]) -> dict:
    """Simulate from the Analysis table's own rows for `days` and the broker
    balance now. The same rows the table shows, so the two cannot disagree on
    which trades there were."""
    table = await _trade_table.closed_trades(engine, days)
    if table.get("error"):
        return {"note": table["error"], "trades": 0}
    pnls = [r.get("pnl") for r in table.get("rows") or []]
    try:
        account = await engine.get_mt5_account()
    except Exception:
        account = None
    balance = float((account or {}).get("balance") or 0.0)
    return await asyncio.to_thread(simulate, pnls, balance, horizon or len(pnls))
