"""What the trade the decision opened actually did (stage 0, second half).

docs/todo/signal-validation/010. Reads the core trade ledger after the fact
and fills in the outcome columns `decision_log` left NULL.

WHY IT READS RATHER THAN BEING TOLD
-----------------------------------
**The close path is frozen.** A research log has no business on it, and
nothing here goes near it. `pro_outcome` takes the same shape for the same
reason -- it walks candles from a cursor rather than being notified when
something closes -- and it is also what lets this sweep be re-run, resumed,
or run against rows written before it existed.

WHAT R MEANS HERE
-----------------
Realised R of OUR execution: `net_pnl / initial_risk`, both from the
ledger, after the template's stop, the Logic-Keyword overrides, the partials
and DPM. **Not** whether the signal's own stated TP1 was touched.

That distinction is the reason this table exists at all. Over the 1,075
snapshots labelled on the channels' stated levels the split is 738 win /
84 loss -- 90% positive, because touching TP1 on gold intraday noise is
close to free -- while over our own 183 executed Telegram trades, 95 never
reached any target and the ones that stopped at TP1 were net negative. A
model trained on the first label approves the entire losing half. See
docs/todo/signal-validation/000 section 4b.
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.src.services.signals import decision_log_repo as _repo

log = logging.getLogger(__name__)


def _get_trade(trade_id: str) -> Optional[dict]:
    """The ledger row, or None. A seam, so the sweep's own behaviour can be
    tested without a trade database."""
    from backend.src.services.trading import trade_repo
    return trade_repo.get_trade(trade_id) or None


def _realised_r(net_pnl: Optional[float], initial_risk: Optional[float]) -> Optional[float]:
    """R, or None when the risk taken is unknown.

    None rather than 0.0: a trade whose risk was never recorded has no R,
    and scoring it flat would put a number we do not have into the mean.
    """
    try:
        risk = float(initial_risk or 0.0)
        if risk <= 0.0 or net_pnl is None:
            return None
        return float(net_pnl) / risk
    except (TypeError, ValueError):
        return None


def resolve_pending(limit: int = 200) -> int:
    """Fill in every decision whose trade has closed. Returns how many were
    resolved.

    Each row is isolated: one unreadable trade must not stop the sweep, and
    an open or missing trade stays in the queue rather than being scored --
    "not known yet" and "flat" are different statements and only one of them
    belongs in a mean.
    """
    resolved = 0
    for row in _repo.unresolved_outcomes(limit):
        try:
            trade = _get_trade(str(row.get("trade_id") or ""))
            if not trade or str(trade.get("status") or "") != "closed":
                continue
            net = trade.get("net_pnl")
            net_f = None if net is None else float(net)
            _repo.set_outcome(
                int(row["id"]),
                outcome="win" if (net_f or 0.0) > 0 else "loss",
                net_usd=net_f,
                realised_r=_realised_r(net_f, trade.get("initial_risk")),
                max_tp_hit=trade.get("max_tp_hit"),
                exit_reason=trade.get("exit_reason"),
            )
            resolved += 1
        except Exception:
            log.debug("[DecisionOutcomes] resolve failed for %s",
                      row.get("trade_id"), exc_info=True)
    return resolved
