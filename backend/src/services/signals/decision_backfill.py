"""Decisions the app made before the log existed (stage 0, retrospective).

docs/todo/signal-validation/010. The log starts empty, and at this account's
volume it needs four to six weeks before a variant has enough resolved rows
to say anything. Most of that wait is avoidable: the trades themselves are
already on record, and two of the five shadow facts can be rebuilt EXACTLY
rather than approximately.

WHAT IS EXACT, AND WHY IT IS NOT A GUESS
----------------------------------------
  * **Session liquidity** is `session_liquidity.check(decided_at)` -- pure
    arithmetic on a timestamp. It returns the same answer today that the
    live gate would have returned at the time, because it never looked at
    the market to begin with.
  * **The entry trigger** replays `candles_range` ending at the decision, so
    it sees the bars that existed then. See `decision_shadow._trigger_blocks`
    for why that means it has no freshness limit.

WHAT IS GONE, AND IS RECORDED AS GONE
-------------------------------------
  * **The event calendar** as it stood is not recoverable. It abstains.
  * **The HTF bias** was a live read. It abstains -- `evaluate_pending`
    already refuses to score it outside MAX_BIAS_LAG_S, so a backfilled row
    gets that for free.
  * **The spread** is taken from `trade_spread_cache`, which is the spread
    on the FILL rather than the quote the guard would have read moments
    earlier. Those are the same number to any precision that matters here,
    but they are not the same measurement, and `source='backfill'` is how a
    reader can tell.

ONLY EXECUTED TRADES
--------------------
Nothing recorded what was blocked before this existed, and blocks cannot be
inferred from an absence -- a signal with no trade might have been gated,
might have been a duplicate, might never have parsed. Inventing them would
hand every challenger a pile of avoided losses it never earned, and make all
of them beat the champion for free.

So a backfilled corpus answers exactly one question, which happens to be the
useful one: **of the trades this account actually took, which would each
gate have stood aside from?**
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.src.services.risk import session_liquidity as _liquidity
from backend.src.services.signals import decision_log_repo as _repo
from backend.src.services.signals import tg_repo as _tg_repo

log = logging.getLogger(__name__)


def _past_telegram_trades(limit: int = 2000) -> list[dict]:
    """Closed Telegram-sourced trades with a message id. A seam, so the
    reconstruction can be tested without a trade database."""
    return _tg_repo.telegram_trades_for_backfill(limit)


def _liquidity_blocked(decided_at: float) -> Optional[int]:
    try:
        ok, _reason = _liquidity.check(float(decided_at), _liquidity.Config())
        return 0 if ok else 1
    except Exception:
        return None


def run(limit: int = 2000) -> int:
    """Rebuild what can be rebuilt. Returns how many rows were added.

    Idempotent through the table's own UNIQUE(tg_message_id, path): running
    it twice adds nothing, and it will never overwrite a live row for a
    message the log already observed -- an observation beats a
    reconstruction of the same thing, and the observation is the one already
    there.
    """
    added = 0
    for trade in _past_telegram_trades(limit):
        try:
            if str(trade.get("status") or "") != "closed":
                continue
            decided_at = float(trade.get("opened_at") or 0.0)
            if decided_at <= 0:
                continue

            before = {r["id"] for r in _repo.rows()}
            decision_id = _repo.insert_decision({
                "tg_message_id": str(trade.get("tg_message_id") or ""),
                "path": "auto",
                "channel_name": trade.get("channel_name"),
                "direction": (trade.get("direction") or "").upper() or None,
                "decided_at": decided_at,
                "executed": 1,
                "skip_reason": "",
                "strategy": trade.get("strategy"),
                "entry_low": trade.get("entry_low"),
                "entry_high": trade.get("entry_high"),
                "stop_loss": trade.get("stop_loss"),
                "tp1": trade.get("tp1"),
                "bid": None, "ask": None,
                "spread_points": trade.get("spread_points"),
                "price": trade.get("entry_price"),
                "session": None,
                "trade_id": trade.get("trade_id"),
                "account_env": None,
                "liquidity_blocked": _liquidity_blocked(decided_at),
                "event_blocked": None,
                "source": "backfill",
            })
            if decision_id is None or decision_id in before:
                continue  # already present, live or backfilled

            net = trade.get("net_pnl")
            net_f = None if net is None else float(net)
            risk = trade.get("initial_risk")
            _repo.set_outcome(
                decision_id,
                outcome="win" if (net_f or 0.0) > 0 else "loss",
                net_usd=net_f,
                realised_r=(float(net_f) / float(risk))
                           if (net_f is not None and risk and float(risk) > 0)
                           else None,
                max_tp_hit=trade.get("max_tp_hit"),
                exit_reason=trade.get("exit_reason"),
            )
            added += 1
        except Exception:
            log.debug("[DecisionBackfill] row failed", exc_info=True)
    if added:
        log.info("[DecisionBackfill] reconstructed %d past decision(s)", added)
    return added
