"""Volatility-target sizing for the Reversal Engine, behind its own switch.

docs/todo/016 phase 2, item 5. `vol_target_sizing_enabled` sat on the
Reversal Engine card from 2026-09-11 with nothing calling
`sizing_policy.apply`. This is the one place that calls it.

**Off (the default), `scale_lot` returns the lot it was given and touches
nothing else.** On, and only for a Reversal Engine signal whose lot was
derived from risk (not a deliberate fixed lot), it applies:

  * the volatility scalar: reference ATR / current ATR, clamped to
    [0.25, 1.5], from `vol_regime` -- the same five-day median the ML
    feature uses, so the two cannot disagree about what "normal" is;
  * the drawdown scalar (one switch with volatility: `capability_gates.
    sizing_inputs` says why);
  * the account's Max lot size, last.

The correlated-exposure cap is deliberately NOT applied. Nothing counts the
open correlated lots yet, and a cap fed 0.0 open lots would look enforced
while enforcing nothing.

The fixed-lot exemption is the channel multiplier's (`resolution.py`): a
typed override, a lot stored on the signal, a template's fixed anchor, and
Fixed lots mode are deliberate values and are never scaled.

Changing a lot is a money change: turn this on during a demo session first.

**The evidence is against turning it on.** docs/simon-handover/040 (closed
by the owner 2026-10-07, "leave it off"): on 5,116 closed Reversal signals the
volatility scalar would have lost $1,002 more than a flat size change at the
same total exposure, because on this book the most volatile decile is the
least bad. The owner chose (2026-10-11) to keep it wired and off with that
result on the card, rather than unwired.

**The drawdown arm reads 0 today.** `get_equity_drawdown_pct` selects a
`peak_balance` column that `vantage_simulation_account` no longer has
(bugs/042 moved the watermark to app_config), so it returns 0.0 and the
drawdown scalar is always 1.0. Not fixed here: the same function feeds the ML
feature `equity_drawdown_pct`, 0 in every stored vector.
"""
from __future__ import annotations

import dataclasses
import logging

from backend.src.services.risk import capability_gates as _caps
from backend.src.services.risk import sizing_policy as _policy

_log = logging.getLogger(__name__)

SOURCE = "Reversal Engine"


def _drawdown_pct() -> float:
    from backend.src.db import database as _db
    try:
        return float(_db.get_equity_drawdown_pct() or 0.0)
    except Exception:
        return 0.0


async def scale_lot(lot: float, rs: dict, *, source: str, fixed: bool) -> float:
    """`lot`, scaled by the sizing policy when the switch is on and this is
    a risk-derived Reversal Engine lot; otherwise `lot` unchanged."""
    if not _caps._on(rs, "vol_target_sizing_enabled"):
        return lot
    if source != SOURCE or fixed:
        return lot
    from backend.src.db import database as _db
    from backend.src.services.reversal_engine import vol_regime as _vol
    atr = await _db.to_db_thread(_vol.latest_atr)
    ref = await _db.to_db_thread(_vol.reference_atr_now)
    drawdown = await _db.to_db_thread(_drawdown_pct)
    inputs = _caps.sizing_inputs(rs, atr or 0.0, ref or 0.0, drawdown,
                                 open_correlated_lots=0.0)
    inputs = dataclasses.replace(inputs, correlated_cap_lots=0.0)
    result = _policy.apply(lot, inputs)
    if result.lots != lot:
        _log.info("[VolTarget] Reversal Engine lot %.2f -> %.2f (%s)",
                  lot, result.lots, "; ".join(result.notes))
    return result.lots
