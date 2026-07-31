"""Signal update + live-trade propagation -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine.update_signal, as part of
the core/engine.py migration series. See
docs/todo/refactor/core-update-signal-migration/020-*.md.

Calls bridge.modify_order and ea_bridge.EABridge.update_trade -- real MT5
order-modification calls, unchanged from the original. This module
modifies no order itself; it only calls whatever `bridge` (and whatever
ea_bridge singleton is configured) its caller supplies.

Takes `bridge` as an explicit parameter instead of self._bridge.
"""
from __future__ import annotations

import logging
from typing import Any

from forex_trader.core import database as db_module
from backend.src.utils.models import (
    STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_SCALP_RUNNER,
    STRATEGY_BE_RUNNER,
)

log = logging.getLogger(__name__)


async def update_signal(bridge: Any, signal_id: str, updates: dict) -> dict:
    """Update signal fields and propagate changes to any linked open trade."""
    allowed = {"direction", "entry_low", "entry_high", "stop_loss",
               "tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8", "notes"}
    safe = {k: v for k, v in updates.items() if k in allowed}
    if not safe:
        return {"status": "no_changes"}

    with db_module.db() as conn:
        set_clause = ", ".join(f"{k}=?" for k in safe)
        conn.execute(
            f"UPDATE vantage_signals SET {set_clause} WHERE signal_id=?",
            list(safe.values()) + [signal_id],
        )
        trade_row = db_module.row_to_dict(
            conn.execute(
                "SELECT * FROM vantage_simulated_trades "
                "WHERE signal_id=? AND status='open'",
                (signal_id,),
            ).fetchone()
        )

    trade_updated = False
    # Conservative, Conservative Trial, and Scalp Runner calculate their own
    # fixed SL/TP1/TP2 from the actual fill price immediately after open. A
    # follow-up signal must not overwrite those fixed-point levels with the
    # channel's own signal levels — confirmed live on ticket 1556670216:
    # scalp_runner wasn't in this set, so a follow-up Gold Diggers VIP signal
    # overwrote its correct fixed TP1/TP2 (and SL) with the signal's own
    # ladder, which combined with the TP Safety Net's tp1-vs-tp2 bug (see
    # _tp_safety_net_check_trade) to close the trade at breakeven+cost
    # instead of running the intended fixed-point management.
    _NO_PROPAGATE = {STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL,
                      STRATEGY_SCALP_RUNNER}
    if trade_row and trade_row.get("strategy") not in _NO_PROPAGATE:
        trade_fields = {
            k: v for k, v in safe.items()
            if k in ("stop_loss", "tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8")
        }
        if trade_fields:
            with db_module.db() as conn:
                tc = ", ".join(f"{k}=?" for k in trade_fields)
                conn.execute(
                    f"UPDATE vantage_simulated_trades SET {tc} WHERE trade_id=?",
                    list(trade_fields.values()) + [trade_row["trade_id"]],
                )
            mt5_ticket = trade_row.get("mt5_ticket")
            if mt5_ticket:
                _dir   = trade_row.get("direction", "").upper()
                _entry = float(trade_row.get("entry_price") or 0)

                # Validate SL: must be on the correct side of the actual fill price.
                raw_sl = safe.get("stop_loss")
                new_sl = None
                if raw_sl is not None:
                    _sl_f = float(raw_sl)
                    if (_dir == "BUY"  and _sl_f < _entry) or \
                       (_dir == "SELL" and _sl_f > _entry):
                        new_sl = raw_sl
                    else:
                        log.warning(
                            "Signal update: SL %.2f skipped for MT5 — "
                            "wrong side of entry %.2f (%s)",
                            _sl_f, _entry, _dir,
                        )

                # Only propagate TP to MT5 for be_runner — that strategy holds the
                # full position and lets MT5 close at the final TP.  Every other
                # strategy manages partial closes in-app via the tick loop; setting
                # any TP on the MT5 ticket causes it to close the entire position
                # when that level is hit, bypassing the partial-close logic.
                _strategy = trade_row.get("strategy", "")
                new_tp = None
                if _strategy == STRATEGY_BE_RUNNER:
                    for _tp_key in ("tp8", "tp7", "tp6", "tp5", "tp4", "tp3", "tp2", "tp1"):
                        _raw = safe.get(_tp_key)
                        if _raw is not None:
                            _tp_f = float(_raw)
                            if (_dir == "BUY" and _tp_f > _entry) or \
                               (_dir == "SELL" and _tp_f < _entry):
                                new_tp = _raw
                            break
                # Skip the direct broker-side modify_order when the EA is
                # actively managing this trade — its own on-tick logic is
                # the sole source of truth for SL progression, and calling
                # modify_order here races against it independently (this is
                # exactly what happened on ticket 1556670216, combined with
                # the TP Safety Net's own tp1-vs-tp2 bug). The update_trade()
                # call below still correctly refreshes the EA's tp[] array
                # so its own TpCleared() checks see the corrected levels.
                _ea_managing = False
                if trade_row.get("managed_by") == "ea":
                    try:
                        from backend.src.services.broker import ea_bridge as _ea_mod0
                        _ea0 = _ea_mod0.get_instance()
                        _ea_managing = bool(_ea0 is not None and _ea0.is_ea_healthy())
                    except Exception:
                        pass
                if not _ea_managing:
                    try:
                        await bridge.modify_order(
                            int(mt5_ticket), sl=new_sl, tp=new_tp
                        )
                        log.info(
                            "Signal %s update propagated to trade %s ticket=%s sl=%s tp1=%s",
                            signal_id, trade_row["trade_id"], mt5_ticket, new_sl, new_tp,
                        )
                    except Exception as e:
                        log.warning("MT5 modify_order after signal update: %s", e)

                # modify_order() above only touches the broker-side native
                # SL/TP fields — for an EA-managed trade the actual partial-
                # close decision is made against the EA's own in-memory
                # tp[]/hasTp[] snapshot, captured once at open_trade time and
                # never refreshed. Without this, a follow-up TP correction on
                # an IME trade (or a manual TP edit from the UI) would be
                # permanently invisible to the EA's on-tick TpCleared() check.
                if trade_row.get("managed_by") == "ea":
                    try:
                        from backend.src.services.broker import ea_bridge as _ea_mod
                        _ea = _ea_mod.get_instance()
                        if _ea is not None:
                            _new_tps = {
                                n: float(safe[f"tp{n}"])
                                for n in range(1, 9)
                                if safe.get(f"tp{n}") is not None
                            }
                            if _new_tps:
                                sent = await _ea.update_trade(trade_row["trade_id"], _new_tps)
                                log.info(
                                    "EA trade %s TP update %s: %s",
                                    trade_row["trade_id"][:8],
                                    "sent" if sent else "skipped (not EA-tracked)",
                                    _new_tps,
                                )
                    except Exception as e:
                        log.warning("EA update_trade after signal update failed: %s", e)
            trade_updated = True

    return {
        "status": "updated",
        "signal_id": signal_id,
        "fields_updated": list(safe.keys()),
        "trade_updated": trade_updated,
        "trade_id": trade_row.get("trade_id") if trade_row else None,
    }
