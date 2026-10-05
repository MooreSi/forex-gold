"""The Breakout engine's own daily loss stop (bugs/048, option 2).

`daily_loss_stop_usd` had been in the parameter catalogue since July,
describing a stop nothing enforced. This is that stop: once the engine's
closed P&L for the current UTC day is at or below minus the limit, no new
signal is created until midnight UTC. It is per engine and separate from the
account-wide daily-loss halt in risk/governor, which still covers every real
order whichever engine sent it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.src.services.breakout_signal import adaptive_params as ap
from backend.src.services.breakout_signal import breakout_signal_repo as bdb

_log = logging.getLogger(__name__)


def reason_today() -> str:
    """Why generation is stopped for today, or "" if it is not."""
    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        pnl = bdb.closed_pnl_since(midnight)
    except Exception as e:                      # noqa: BLE001
        # Fails open: the account-wide daily-loss halt still guards every real
        # order, and a read error must not silence the engine for the day.
        _log.warning("[BO-Engine] daily loss stop could not read today's P&L: %s", e)
        return ""
    limit = ap.get("daily_loss_stop_usd")
    if limit > 0 and pnl <= -limit:
        return (f"Daily loss stop: today's closed P&L ${pnl:.2f} has reached "
                f"-${limit:.2f} — no new signals until 00:00 UTC")
    return ""


def suppress(log_entry: dict, velocity: bool) -> bool:
    """True when the candidate must be dropped. The M5 cycle records why in
    its analysis log, as its other suppressions do; a velocity candidate
    does not write the cycle's log entry."""
    reason = reason_today()
    if not reason:
        return False
    if not velocity:
        log_entry.update({"result": "daily_loss_stop", "suppressed_reason": reason})
        bdb.log_analysis(log_entry)
    _log.debug("[BO-Engine] suppressed: %s", reason)
    return True
