"""Offline only: no application imports, model persistence or broker access."""
from __future__ import annotations
import numpy as np


def causal_train(rows, test_start: float, embargo_s: float = 3600):
    """Only outcomes already knowable before the test decision, with a gap."""
    return rows[(rows.created_at < test_start) &
                (rows.label_available_at < test_start - embargo_s)]


def net_virtual_r(row):
    # Source: ml_engine/_training_data.py, _realised_r. Net already includes costs.
    if row.get("live_exec_status") == "executed":
        return None
    try:
        risk = float(row.get("sl_dist")) * 10.0
        net = float(row.get("net_pnl_dollars"))
        if risk <= 0 or not np.isfinite(risk) or not np.isfinite(net):
            return None
        return float(np.clip(net / risk, -12, 12))
    except (TypeError, ValueError):
        return None


def select_trades(predictions, margin_r=0.05):
    values = np.asarray(predictions, dtype=float)
    return np.isfinite(values) & (values > margin_r)
