"""Exposure guard for the INTERNAL signal generators only (Reversal Engine,
Breakout Engine, Bounce Engine) -- Trading > Strategy > "Internal Engine
Exposure".

Why this exists
---------------
The internal engines have no self-hedge guard of any kind. Each engine's
own duplicate check (_already_open in reversal_engine_service.py) is scoped
to the SAME direction at a nearby level, and the cross-engine bus check
(core_db_signal_bus.has_conflict_on_bus) calls get_concurrent_signals with
exclude_engine=<itself>, so it only ever sees OTHER engines. Nothing stops
one engine -- or two different internal engines -- holding a BUY and a SELL
on the same instrument simultaneously.

Deliberately defaults to OFF
----------------------------
Measured on 86 closed Reversal Engine trades (2026-07-21..27): opposing
positions that were genuinely open at the same time accounted for 16 trades
(19% of all closed trades) but $463.52 of the engine's $578.01 total profit
-- roughly 80% of all profit. 7 of the 8 overlapping pairs had BOTH legs
close in profit; exactly one pair had a leg cancel the other out (2026-07-22
21:18, SELL +$57.24 / BUY -$72.45, net -$15.21). For a mean-reversion
engine, buying support while selling resistance in a range is the strategy
working as intended, not a fault. So this guard exists because it was asked
for and there are conditions where it's the right call (trending markets,
tighter risk budgets), but turning it on constrains the behaviour that has
so far been the engine's main edge. Off = the long-standing behaviour,
completely unchanged.

Telegram-channel trades are never affected -- this reads and counts only
tg_source values belonging to the internal generators (see
_INTERNAL_SOURCES, which includes the pre-rename variants that
core_db_channel.CANONICAL_CHANNELS folds into the current names, since
historical rows can still carry them).
"""
from __future__ import annotations

import logging

from forex_trader.core import database as db_module

log = logging.getLogger(__name__)

MODE_OFF          = "off"
MODE_SELF_HEDGE   = "self_hedge"
MODE_NET_EXPOSURE = "net_exposure"
MODE_CHOICES      = (MODE_OFF, MODE_SELF_HEDGE, MODE_NET_EXPOSURE)

MODE_LABELS = {
    MODE_OFF:          "Off (no restriction)",
    MODE_SELF_HEDGE:   "Self-Hedge Guard",
    MODE_NET_EXPOSURE: "Net Exposure Cap",
}

# Every tg_source an internal generator's trades can carry, including the
# legacy pre-rename strings still present on historical rows (see
# core_db_channel.CANONICAL_CHANNELS). ORB/IVB is deliberately excluded --
# it is a once-a-day scheduled report with its own dedup, not a continuously
# generating engine.
_INTERNAL_SOURCES = (
    "Reversal Engine", "GD Copy Engine", "Gold Diggers VIP Copy",
    "Breakout Engine",
    "Bounce Engine", "Bounce Generator", "Signal Generator",
)


def _open_internal_legs() -> list[tuple[str, float]]:
    """[(direction, lot_size), ...] for every currently-open trade belonging
    to an internal generator. remaining_lots (not lot_size) is the live
    exposure -- a partially-closed trade no longer carries its original
    size."""
    placeholders = ",".join("?" for _ in _INTERNAL_SOURCES)
    with db_module.db() as conn:
        rows = conn.execute(
            f"SELECT direction, COALESCE(remaining_lots, lot_size) AS lots "
            f"FROM vantage_simulated_trades "
            f"WHERE status='open' AND tg_source IN ({placeholders})",
            _INTERNAL_SOURCES,
        ).fetchall()
    return [((r[0] or "").upper(), float(r[1] or 0)) for r in rows]


def net_internal_exposure() -> float:
    """Signed net lots across all open internal-engine trades: positive =
    net long, negative = net short, 0 = flat/fully hedged."""
    net = 0.0
    for direction, lots in _open_internal_legs():
        net += lots if direction == "BUY" else -lots
    return round(net, 4)


def check_internal_exposure(
    direction: str, lot_size: float, rs: dict | None = None,
) -> tuple[bool, str]:
    """(allowed, reason) for an internal generator about to open `lot_size`
    lots in `direction`. reason is "" when allowed.

    Call this ONLY from the internal engines' live-execution paths --
    Telegram-signal trades are out of scope by design and must never be
    gated by it.
    """
    if rs is None:
        rs = db_module.get_risk_settings()
    mode = (rs.get("internal_hedge_mode") or MODE_OFF).strip()
    if mode == MODE_OFF or mode not in MODE_CHOICES:
        return True, ""

    direction = (direction or "").upper()
    lot_size = float(lot_size or 0)
    legs = _open_internal_legs()

    if mode == MODE_SELF_HEDGE:
        opposing = [l for d, l in legs if d and d != direction]
        if opposing:
            return False, (
                f"Self-Hedge Guard: {len(opposing)} opposing "
                f"{'BUY' if direction == 'SELL' else 'SELL'} position(s) "
                f"({sum(opposing):.2f} lots) already open from an internal engine"
            )
        return True, ""

    # MODE_NET_EXPOSURE -- a hedge is allowed (it REDUCES |net|); what this
    # blocks is stacking further in whichever direction is already dominant.
    # NOTE: parsed WITHOUT the usual `or <default>` idiom -- an explicit 0.0
    # is falsy, so `float(x or 0.30)` would silently turn "0" into 0.30 and
    # quietly re-enable a cap the user had deliberately disabled.
    _raw_cap = rs.get("internal_net_exposure_max_lots")
    cap = float(_raw_cap) if _raw_cap is not None else 0.30
    if cap <= 0:
        return True, ""   # 0 = disabled, matching the app's usual "0 = no cap"
    net = sum(l if d == "BUY" else -l for d, l in legs)
    prospective = net + (lot_size if direction == "BUY" else -lot_size)
    # A trade that moves the book back toward flat is always allowed, even
    # while |net| is still above the cap -- otherwise a book that got over
    # the cap (an earlier cap, a manual trade, a partial close) could never
    # be hedged back down, which is the opposite of this mode's intent.
    if abs(prospective) <= abs(net) - 1e-9:
        return True, ""
    if abs(prospective) > cap + 1e-9:
        return False, (
            f"Net Exposure Cap: this {direction} would take net internal "
            f"exposure to {prospective:+.2f} lots, over the {cap:.2f} cap "
            f"(currently {net:+.2f})"
        )
    return True, ""
