"""The one metric suite. Every experiment reports THIS dict, so results are
comparable across notebooks. All P&L is in R-multiples (risk units): a full
stop-out = -1.0 R. Dollarize only for presentation (x sl_risk_usd, ~$50).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def summarize(r_multiples: pd.Series | list[float], n_candidates: int | None = None) -> dict:
    """r_multiples: one entry per *filled* trade. n_candidates: how many
    signals the config was offered before filtering/expiry (for the
    'improved P&L by deleting all trades' catch)."""
    r = pd.Series(list(r_multiples), dtype=float).dropna()
    n = len(r)
    if n == 0:
        return {"trades": 0, "candidates": n_candidates, "total_R": 0.0,
                "expectancy_R": np.nan, "win_rate": np.nan, "avg_win_R": np.nan,
                "avg_loss_R": np.nan, "payoff_ratio": np.nan,
                "profit_factor": np.nan, "max_drawdown_R": 0.0}
    wins, losses = r[r > 0], r[r < 0]
    equity = r.cumsum()
    dd = float((equity - equity.cummax()).min())
    pf = float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() != 0 else np.inf
    return {
        "trades": n,
        "candidates": n_candidates,
        "total_R": round(float(r.sum()), 2),
        "expectancy_R": round(float(r.mean()), 4),
        "win_rate": round(float((r > 0).mean()), 3),
        "avg_win_R": round(float(wins.mean()), 3) if len(wins) else 0.0,
        "avg_loss_R": round(float(losses.mean()), 3) if len(losses) else 0.0,
        "payoff_ratio": round(float(wins.mean() / -losses.mean()), 3)
        if len(wins) and len(losses) else np.nan,
        "profit_factor": round(pf, 3),
        "max_drawdown_R": round(dd, 2),
    }


def summary_table(named_runs: dict[str, dict]) -> pd.DataFrame:
    """named_runs: {config_name: summarize(...) dict} -> comparison table."""
    return pd.DataFrame(named_runs).T


def auc(labels: pd.Series | list[int], scores: pd.Series | list[float]) -> float:
    """Rank AUC (probability a random positive outranks a random negative).
    0.5 = useless, <0.5 = inverted. No sklearn needed."""
    df = pd.DataFrame({"y": list(labels), "s": list(scores)}).dropna()
    pos = df[df.y == 1]["s"].to_numpy()
    neg = df[df.y == 0]["s"].to_numpy()
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    wins = (pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum()
    return float(wins / (len(pos) * len(neg)))
