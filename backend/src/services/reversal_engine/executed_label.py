"""A training label for a Reversal signal that was really traded.

docs/todo/017 item 2. `_realised_r` refuses executed rows because their
`net_pnl_dollars` is the broker's, at the real lot under the EA template's
stop, and the row holds neither (reversal-engine/250). 1,025 of 7,626 rows on
the owner's Mac, so the model never learned from a single real trade.

The core trade row already holds the figure the label needs:
`vantage_simulated_trades.initial_risk` is the money at risk at the stop
actually sent, across every leg of a grid, refined by `core_profit_sync` once
the legs settle (migration 26). Its `net_pnl` sums the same legs. So the
label is that row's net over that row's risk, on the same R scale as the
virtual label and clamped the same way. No write on the order path is needed.

Joined by `re_signals.vantage_signal_id` = `vantage_simulated_trades.signal_id`.
Joining by ticket double-counts: on the 2026-10-14 copy, 519 executed signals
matched 744 trade rows by ticket, 316 matched one row each by signal id. A
signal that matches more than one closed row is left out rather than guessed.
Only trades since the core database last started carry `initial_risk`, so
older executed rows stay unlabelled.

Read-only. Nothing here places, closes or modifies a trade.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

from backend.src.services.reversal_engine.ml_engine._training_data import (
    _R_LABEL_CLAMP)


def label(net: Optional[float], risk: Optional[float]) -> Optional[float]:
    """net / risk, clamped, or None when either cannot say."""
    try:
        n, r = float(net), float(risk)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(n) and math.isfinite(r)) or r <= 0:
        return None
    return max(-_R_LABEL_CLAMP, min(_R_LABEL_CLAMP, n / r))


def fetch_closed_risk(vantage_signal_ids: Iterable[str]) -> dict:
    """{vantage_signal_id: (net_pnl, initial_risk)} for each id with exactly
    one closed core trade that recorded its risk. The SQL is in the repo."""
    from backend.src.services.reversal_engine import executed_label_repo as _repo
    return _repo.closed_risk_by_signal(vantage_signal_ids)
