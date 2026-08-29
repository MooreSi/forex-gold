"""Per-strategy backtest simulators -- split verbatim from engine.py
(M2 file-size pass). Each _simulate_* walks candles for one signal under one
strategy's management rules; engine.py's _simulate dispatcher calls them.
Constants, dataclasses and math helpers stay in engine.py and are imported
back here -- engine.py imports this module lazily inside _simulate, so there
is no import cycle.
"""

from __future__ import annotations

import math
from typing import Optional

from backend.src.services.backtest.engine import (
    BtSignal, BtTrade,
    _close_trade, _lot_size, _valid_tp, _atr14, _ema_closes,
    _USD_PER_PT_PER_LOT, _MAX_HOLD_BARS, _MIN_LOT, _MAX_LOT,
    _TRAIL_DIST_PTS,
    _GDVR_SL_MULT, _GDVR_SL_CAP_PT, _GDVR_SL_FLOOR_PT, _GDVR_MAX_HOLD_BARS,
    _GDVR_PCTS, _CLIMBER_PCTS,
    _ADAPTIVE_SL_MULT, _ADAPTIVE_SL_CAP_PT, _ADAPTIVE_SL_FLOOR_PT,
    _ADAPTIVE_TP_CAP_FRAC, _ADAPTIVE_MAX_HOLD_BARS,
)


def _simulate_conservative(
    candles: list[dict], sig: BtSignal, fill_bar: int, fill_price: float,
    is_buy: bool, balance: float, risk_pct: float, fixed_lots: float = 0.0,
) -> BtTrade:
    """Conservative: 5-pt SL / 3-pt TP1 from fill; 80% close at TP1; 3-pt trailing stop."""
    d          = 1.0 if is_buy else -1.0
    sl_dist    = 5.0
    tp1_dist   = 3.0
    trail_dist = 3.0
    sl         = fill_price - d * sl_dist
    tp1        = fill_price + d * tp1_dist
    lot        = _lot_size(balance, sl_dist, risk_pct, fixed_lots)

    trade = BtTrade(
        signal_id=sig.signal_id, strategy="conservative", direction=sig.direction,
        fill_price=fill_price, fill_bar_idx=fill_bar, lot_size=lot,
    )

    current_sl    = sl
    remaining_lot = lot
    partial_pnl   = 0.0
    tp1_hit       = False
    end_bar       = min(fill_bar + _MAX_HOLD_BARS, len(candles))

    for i in range(fill_bar, end_bar):
        c = candles[i]
        if c["low"] <= current_sl if is_buy else c["high"] >= current_sl:
            return _close_trade(trade, current_sl, i, fill_bar, is_buy, remaining_lot,
                                partial_pnl, "tp1_only" if tp1_hit else "sl")

        if not tp1_hit and (c["high"] >= tp1 if is_buy else c["low"] <= tp1):
            tp1_hit       = True
            tp1_move      = (tp1 - fill_price) if is_buy else (fill_price - tp1)
            close_lot     = round(lot * 0.80, 2)
            partial_pnl  += tp1_move * close_lot * _USD_PER_PT_PER_LOT
            remaining_lot = round(lot - close_lot, 2)
            current_sl    = fill_price

        if tp1_hit:
            if is_buy:
                new_sl = max(round(c["close"] - trail_dist, 2), fill_price)
                current_sl = max(current_sl, new_sl)
            else:
                new_sl = min(round(c["close"] + trail_dist, 2), fill_price)
                current_sl = min(current_sl, new_sl)

    # Out of bars with the position still open. Priced at the MARKET,
    # not at the fill: closing at fill_price reports every timed-out
    # trade as break-even, which flatters any strategy whose trades run
    # long and drift. _run_ladder_strategy always did it this way; the
    # other simulators did not (bugs/017).
    last_close = candles[end_bar - 1]["close"]
    move       = (last_close - fill_price) if is_buy else (fill_price - last_close)
    trade.close_price   = last_close
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = move
    trade.pnl_usd       = partial_pnl + move * remaining_lot * _USD_PER_PT_PER_LOT
    trade.outcome       = "timeout"
    return trade


def _simulate_ct(
    candles: list[dict], sig: BtSignal, fill_bar: int, fill_price: float,
    is_buy: bool, balance: float, risk_pct: float, fixed_lots: float = 0.0,
) -> BtTrade:
    """Conservative Trial: fixed 10-pt SL; 6 graduated TP levels from fill."""
    d         = 1.0 if is_buy else -1.0
    sl_dist   = 10.0
    sl        = fill_price - d * sl_dist
    lot       = _lot_size(balance, sl_dist, risk_pct, fixed_lots)

    tp_levels = [fill_price + d * p for p in (5.0, 10.0, 14.0, 20.0, 27.0, 35.0)]
    tp_pcts   = (0.05, 0.30, 0.20, 0.40, 0.05, 1.00)

    trade = BtTrade(
        signal_id=sig.signal_id, strategy="conservative_trial", direction=sig.direction,
        fill_price=fill_price, fill_bar_idx=fill_bar, lot_size=lot,
    )

    current_sl    = sl
    remaining_lot = lot
    partial_pnl   = 0.0
    tp_idx        = 0
    end_bar       = min(fill_bar + _MAX_HOLD_BARS, len(candles))

    for i in range(fill_bar, end_bar):
        c = candles[i]
        if c["low"] <= current_sl if is_buy else c["high"] >= current_sl:
            return _close_trade(trade, current_sl, i, fill_bar, is_buy, remaining_lot,
                                partial_pnl, "tp1_only" if tp_idx >= 2 else "sl")

        while tp_idx < len(tp_levels):
            tp_val = tp_levels[tp_idx]
            if not (c["high"] >= tp_val if is_buy else c["low"] <= tp_val):
                break
            pct       = tp_pcts[tp_idx]
            close_lot = remaining_lot if tp_idx == 5 else round(lot * pct, 2)
            close_lot = max(0.0, min(close_lot, remaining_lot))
            tp_move   = (tp_val - fill_price) if is_buy else (fill_price - tp_val)
            partial_pnl   += tp_move * close_lot * _USD_PER_PT_PER_LOT
            remaining_lot  = round(remaining_lot - close_lot, 2)
            if tp_idx == 1:
                current_sl = fill_price
            elif tp_idx == 3:
                current_sl = tp_levels[1]
            tp_idx += 1
            if remaining_lot <= 0:
                trade.close_price   = tp_val
                trade.close_bar_idx = i
                trade.hold_bars     = i - fill_bar
                trade.pnl_pts       = tp_move
                trade.pnl_usd       = partial_pnl
                trade.outcome       = "tp3_direct"
                return trade

    # Out of bars with the position still open. Priced at the MARKET,
    # not at the fill: closing at fill_price reports every timed-out
    # trade as break-even, which flatters any strategy whose trades run
    # long and drift. _run_ladder_strategy always did it this way; the
    # other simulators did not (bugs/017).
    last_close = candles[end_bar - 1]["close"]
    move       = (last_close - fill_price) if is_buy else (fill_price - last_close)
    trade.close_price   = last_close
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = move
    trade.pnl_usd       = partial_pnl + move * remaining_lot * _USD_PER_PT_PER_LOT
    trade.outcome       = "timeout"
    return trade


def _simulate_nss(
    candles: list[dict], sig: BtSignal, fill_bar: int, fill_price: float,
    is_buy: bool, sig_sl_dist: float, balance: float, risk_pct: float,
    fixed_lots: float = 0.0,
) -> BtTrade:
    """Trend Ratchet: 1.5× emergency stop; 20% closes at TP1 and TP3; SL ratchets to TP1."""
    emg_dist  = sig_sl_dist * 1.5
    emg_sl    = (fill_price - emg_dist) if is_buy else (fill_price + emg_dist)
    lot       = _lot_size(balance, emg_dist, risk_pct, fixed_lots)
    tp1       = _valid_tp(sig.tp1, is_buy, fill_price)
    tp3       = _valid_tp(sig.tp3, is_buy, fill_price)

    trade = BtTrade(
        signal_id=sig.signal_id, strategy="no_sl_scale", direction=sig.direction,
        fill_price=fill_price, fill_bar_idx=fill_bar, lot_size=lot,
    )

    current_sl    = emg_sl
    remaining_lot = lot
    partial_pnl   = 0.0
    tp1_hit       = False
    tp3_hit       = False
    end_bar       = min(fill_bar + _MAX_HOLD_BARS, len(candles))

    for i in range(fill_bar, end_bar):
        c = candles[i]
        if c["low"] <= current_sl if is_buy else c["high"] >= current_sl:
            outcome = "tp1_tp3" if tp3_hit else ("tp1_only" if tp1_hit else "sl")
            return _close_trade(trade, current_sl, i, fill_bar, is_buy, remaining_lot, partial_pnl, outcome)

        if tp1 is not None and not tp1_hit:
            if c["high"] >= tp1 if is_buy else c["low"] <= tp1:
                tp1_hit       = True
                tp1_move      = (tp1 - fill_price) if is_buy else (fill_price - tp1)
                close_lot     = round(lot * 0.20, 2)
                partial_pnl  += tp1_move * close_lot * _USD_PER_PT_PER_LOT
                remaining_lot = round(remaining_lot - close_lot, 2)

        if tp3 is not None and not tp3_hit and tp1_hit:
            if c["high"] >= tp3 if is_buy else c["low"] <= tp3:
                tp3_hit       = True
                tp3_move      = (tp3 - fill_price) if is_buy else (fill_price - tp3)
                close_lot     = round(lot * 0.20, 2)
                partial_pnl  += tp3_move * close_lot * _USD_PER_PT_PER_LOT
                remaining_lot = round(remaining_lot - close_lot, 2)
                if tp1 is not None:
                    current_sl = tp1

    # Out of bars with the position still open. Priced at the MARKET,
    # not at the fill: closing at fill_price reports every timed-out
    # trade as break-even, which flatters any strategy whose trades run
    # long and drift. _run_ladder_strategy always did it this way; the
    # other simulators did not (bugs/017).
    last_close = candles[end_bar - 1]["close"]
    move       = (last_close - fill_price) if is_buy else (fill_price - last_close)
    trade.close_price   = last_close
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = move
    trade.pnl_usd       = partial_pnl + move * remaining_lot * _USD_PER_PT_PER_LOT
    trade.outcome       = "timeout"
    return trade


def _simulate_be_runner(
    candles: list[dict], sig: BtSignal, fill_bar: int, fill_price: float,
    is_buy: bool, sig_sl_dist: float, balance: float, risk_pct: float,
    fixed_lots: float = 0.0,
) -> BtTrade:
    """Breakeven Runner: no partial closes; SL steps to previous TP at each TP cleared."""
    lot = _lot_size(balance, sig_sl_dist, risk_pct, fixed_lots)
    tp1 = _valid_tp(sig.tp1, is_buy, fill_price)
    tp2 = _valid_tp(sig.tp2, is_buy, fill_price)
    tp3 = _valid_tp(sig.tp3, is_buy, fill_price)

    trade = BtTrade(
        signal_id=sig.signal_id, strategy="be_runner", direction=sig.direction,
        fill_price=fill_price, fill_bar_idx=fill_bar, lot_size=lot,
    )

    current_sl = sig.stop_loss
    tp1_hit    = False
    tp2_hit    = False
    end_bar    = min(fill_bar + _MAX_HOLD_BARS, len(candles))

    for i in range(fill_bar, end_bar):
        c = candles[i]
        if c["low"] <= current_sl if is_buy else c["high"] >= current_sl:
            return _close_trade(trade, current_sl, i, fill_bar, is_buy, lot, 0.0,
                                "tp1_only" if tp1_hit else "sl")

        if tp1 is not None and not tp1_hit:
            if c["high"] >= tp1 if is_buy else c["low"] <= tp1:
                tp1_hit    = True
                current_sl = fill_price

        if tp2 is not None and not tp2_hit and tp1_hit:
            if c["high"] >= tp2 if is_buy else c["low"] <= tp2:
                tp2_hit    = True
                current_sl = tp1

        if tp3 is not None and tp1_hit:
            if c["high"] >= tp3 if is_buy else c["low"] <= tp3:
                tp3_move = (tp3 - fill_price) if is_buy else (fill_price - tp3)
                trade.close_price   = tp3
                trade.close_bar_idx = i
                trade.hold_bars     = i - fill_bar
                trade.pnl_pts       = tp3_move
                trade.pnl_usd       = tp3_move * lot * _USD_PER_PT_PER_LOT
                trade.outcome       = "tp1_tp3"
                return trade

    # Out of bars with the position still open. Priced at the MARKET,
    # not at the fill: closing at fill_price reports every timed-out
    # trade as break-even, which flatters any strategy whose trades run
    # long and drift. _run_ladder_strategy always did it this way; the
    # other simulators did not (bugs/017).
    last_close = candles[end_bar - 1]["close"]
    move       = (last_close - fill_price) if is_buy else (fill_price - last_close)
    trade.close_price   = last_close
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = move
    trade.pnl_usd       = move * lot * _USD_PER_PT_PER_LOT
    trade.outcome       = "timeout"
    return trade


def _simulate_scale_out(
    candles: list[dict], sig: BtSignal, fill_bar: int, fill_price: float,
    is_buy: bool, sig_sl_dist: float, balance: float, risk_pct: float,
    fixed_lots: float = 0.0,
) -> BtTrade:
    """Scale Out: 40% at TP1 (SL→BE), 30% at TP2, remaining at TP3."""
    lot = _lot_size(balance, sig_sl_dist, risk_pct, fixed_lots)
    tp1 = _valid_tp(sig.tp1, is_buy, fill_price)
    tp2 = _valid_tp(sig.tp2, is_buy, fill_price)
    tp3 = _valid_tp(sig.tp3, is_buy, fill_price)

    trade = BtTrade(
        signal_id=sig.signal_id, strategy="scale_out", direction=sig.direction,
        fill_price=fill_price, fill_bar_idx=fill_bar, lot_size=lot,
    )

    current_sl    = sig.stop_loss
    remaining_lot = lot
    partial_pnl   = 0.0
    tp1_hit       = False
    tp2_hit       = False
    end_bar       = min(fill_bar + _MAX_HOLD_BARS, len(candles))

    for i in range(fill_bar, end_bar):
        c = candles[i]
        if c["low"] <= current_sl if is_buy else c["high"] >= current_sl:
            return _close_trade(trade, current_sl, i, fill_bar, is_buy, remaining_lot,
                                partial_pnl, "tp1_only" if tp1_hit else "sl")

        if tp1 is not None and not tp1_hit:
            if c["high"] >= tp1 if is_buy else c["low"] <= tp1:
                tp1_hit       = True
                tp1_move      = (tp1 - fill_price) if is_buy else (fill_price - tp1)
                close_lot     = round(lot * 0.40, 2)
                partial_pnl  += tp1_move * close_lot * _USD_PER_PT_PER_LOT
                remaining_lot = round(remaining_lot - close_lot, 2)
                current_sl    = fill_price

        if tp2 is not None and not tp2_hit and tp1_hit:
            if c["high"] >= tp2 if is_buy else c["low"] <= tp2:
                tp2_hit       = True
                tp2_move      = (tp2 - fill_price) if is_buy else (fill_price - tp2)
                close_lot     = round(lot * 0.30, 2)
                partial_pnl  += tp2_move * close_lot * _USD_PER_PT_PER_LOT
                remaining_lot = round(remaining_lot - close_lot, 2)

        if tp3 is not None and tp1_hit:
            if c["high"] >= tp3 if is_buy else c["low"] <= tp3:
                tp3_move = (tp3 - fill_price) if is_buy else (fill_price - tp3)
                trade.close_price   = tp3
                trade.close_bar_idx = i
                trade.hold_bars     = i - fill_bar
                trade.pnl_pts       = tp3_move
                trade.pnl_usd       = partial_pnl + tp3_move * remaining_lot * _USD_PER_PT_PER_LOT
                trade.outcome       = "tp1_tp3"
                return trade

    # Out of bars with the position still open. Priced at the MARKET,
    # not at the fill: closing at fill_price reports every timed-out
    # trade as break-even, which flatters any strategy whose trades run
    # long and drift. _run_ladder_strategy always did it this way; the
    # other simulators did not (bugs/017).
    last_close = candles[end_bar - 1]["close"]
    move       = (last_close - fill_price) if is_buy else (fill_price - last_close)
    trade.close_price   = last_close
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = move
    trade.pnl_usd       = partial_pnl + move * remaining_lot * _USD_PER_PT_PER_LOT
    trade.outcome       = "timeout"
    return trade


def _simulate_protected_scale(
    candles: list[dict], sig: BtSignal, fill_bar: int, fill_price: float,
    is_buy: bool, sig_sl_dist: float, balance: float, risk_pct: float,
    fixed_lots: float = 0.0,
) -> BtTrade:
    """Protected Scale: hold through TP1/TP2; SL → BE at TP2; close from TP3 onward."""
    lot = _lot_size(balance, sig_sl_dist, risk_pct, fixed_lots)
    tp1 = _valid_tp(sig.tp1, is_buy, fill_price)
    tp2 = _valid_tp(sig.tp2, is_buy, fill_price)
    tp3 = _valid_tp(sig.tp3, is_buy, fill_price)

    trade = BtTrade(
        signal_id=sig.signal_id, strategy="protected_scale", direction=sig.direction,
        fill_price=fill_price, fill_bar_idx=fill_bar, lot_size=lot,
    )

    current_sl    = sig.stop_loss
    remaining_lot = lot
    partial_pnl   = 0.0
    tp1_hit       = False
    tp2_hit       = False
    end_bar       = min(fill_bar + _MAX_HOLD_BARS, len(candles))

    for i in range(fill_bar, end_bar):
        c = candles[i]
        if c["low"] <= current_sl if is_buy else c["high"] >= current_sl:
            return _close_trade(trade, current_sl, i, fill_bar, is_buy, remaining_lot,
                                partial_pnl, "tp1_only" if tp2_hit else "sl")

        if tp1 is not None and not tp1_hit:
            if c["high"] >= tp1 if is_buy else c["low"] <= tp1:
                tp1_hit = True

        if tp2 is not None and not tp2_hit and tp1_hit:
            if c["high"] >= tp2 if is_buy else c["low"] <= tp2:
                tp2_hit    = True
                current_sl = fill_price

        if tp3 is not None and tp2_hit:
            if c["high"] >= tp3 if is_buy else c["low"] <= tp3:
                tp3_move = (tp3 - fill_price) if is_buy else (fill_price - tp3)
                trade.close_price   = tp3
                trade.close_bar_idx = i
                trade.hold_bars     = i - fill_bar
                trade.pnl_pts       = tp3_move
                trade.pnl_usd       = partial_pnl + tp3_move * remaining_lot * _USD_PER_PT_PER_LOT
                trade.outcome       = "tp1_tp3"
                return trade

    # Out of bars with the position still open. Priced at the MARKET,
    # not at the fill: closing at fill_price reports every timed-out
    # trade as break-even, which flatters any strategy whose trades run
    # long and drift. _run_ladder_strategy always did it this way; the
    # other simulators did not (bugs/017).
    last_close = candles[end_bar - 1]["close"]
    move       = (last_close - fill_price) if is_buy else (fill_price - last_close)
    trade.close_price   = last_close
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = move
    trade.pnl_usd       = partial_pnl + move * remaining_lot * _USD_PER_PT_PER_LOT
    trade.outcome       = "timeout"
    return trade


def _simulate_trail_stop(
    candles: list[dict], sig: BtSignal, fill_bar: int, fill_price: float,
    is_buy: bool, sig_sl_dist: float, balance: float, risk_pct: float,
    fixed_lots: float = 0.0,
) -> BtTrade:
    """Trailing Stop: no partials; fixed 5-pt trail activates at TP1."""
    lot = _lot_size(balance, sig_sl_dist, risk_pct, fixed_lots)
    tp1 = _valid_tp(sig.tp1, is_buy, fill_price)

    trade = BtTrade(
        signal_id=sig.signal_id, strategy="trail_stop", direction=sig.direction,
        fill_price=fill_price, fill_bar_idx=fill_bar, lot_size=lot,
    )

    current_sl = sig.stop_loss
    tp1_hit    = False
    end_bar    = min(fill_bar + _MAX_HOLD_BARS, len(candles))

    for i in range(fill_bar, end_bar):
        c = candles[i]
        if c["low"] <= current_sl if is_buy else c["high"] >= current_sl:
            return _close_trade(trade, current_sl, i, fill_bar, is_buy, lot, 0.0,
                                "tp1_only" if tp1_hit else "sl")

        if tp1 is not None and not tp1_hit:
            if c["high"] >= tp1 if is_buy else c["low"] <= tp1:
                tp1_hit    = True
                current_sl = fill_price

        if tp1_hit:
            if is_buy:
                new_sl = max(round(c["close"] - _TRAIL_DIST_PTS, 2), fill_price)
                current_sl = max(current_sl, new_sl)
            else:
                new_sl = min(round(c["close"] + _TRAIL_DIST_PTS, 2), fill_price)
                current_sl = min(current_sl, new_sl)

    # Out of bars with the position still open. Priced at the MARKET,
    # not at the fill: closing at fill_price reports every timed-out
    # trade as break-even, which flatters any strategy whose trades run
    # long and drift. _run_ladder_strategy always did it this way; the
    # other simulators did not (bugs/017).
    last_close = candles[end_bar - 1]["close"]
    move       = (last_close - fill_price) if is_buy else (fill_price - last_close)
    trade.close_price   = last_close
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = move
    trade.pnl_usd       = move * lot * _USD_PER_PT_PER_LOT
    trade.outcome       = "timeout"
    return trade


def _run_ladder_strategy(
    candles: list[dict], sig: BtSignal, fill_bar: int, fill_price: float,
    is_buy: bool, sl_dist: float, lot: float, strategy_tag: str,
    pcts_table: dict[int, list[float]], be_at_pos: int, max_hold_bars: int,
) -> BtTrade:
    """
    Shared TP-ladder walk used by Reversal Runner, Signal Climber, and Adaptive
    Runner. Closes fractions of the original lot at each of the signal's
    ACTUAL TPs (looked up from pcts_table by however many TPs the signal has —
    e.g. a 3-TP signal uses pcts_table[3], which sums to 1.0 over exactly
    those 3 levels), trailing SL to breakeven at position `be_at_pos` (0 =
    TP1) then to the previous TP price after every subsequent TP. SL is
    checked before TPs each bar (conservative — assumes adverse touches
    resolve before favourable ones within the same bar).

    This replaces the old fixed 8-slot-by-*position* ladder, which silently
    dropped most of the position for any signal with fewer than 8 TPs (a
    3-TP signal only ever hit ladder slots 0-2, closing 20% of the lot and
    leaving 80% open with nothing left to close it against).

    Only TPs on the correct side of the fill price are kept — mirrors
    engine.py's live _run_tp_ladder, which filters the same way. Without
    this, a corrupt TP value in the signal data (confirmed live: a stored
    tp2 of 40.0 for a BUY filled at 4048 — clearly a truncated/mis-parsed
    price, not a real target near 4048) reads as "instantly hit" on the very
    first bar (any real price is >= 40.0), and the ladder banks a multi-
    thousand-point "move" against the fill price as if it were a real trade
    outcome. This produced several -$1,500 to -$3,700 phantom losses in an
    otherwise-normal backtest run (2026-07-15) before this filter was added.
    """
    tps = [
        t for t in sig.tps if t is not None
        and ((is_buy and t > fill_price) or (not is_buy and t < fill_price))
    ]
    tp_count = len(tps)
    fracs = pcts_table.get(tp_count) or pcts_table[max(pcts_table)][:tp_count]

    sl_price = fill_price - (1.0 if is_buy else -1.0) * sl_dist

    trade = BtTrade(
        signal_id=sig.signal_id, strategy=strategy_tag, direction=sig.direction,
        fill_price=fill_price, fill_bar_idx=fill_bar, lot_size=lot,
    )

    tp_done       = [False] * tp_count
    remaining_lot = lot
    partial_pnl   = 0.0
    end_bar       = min(fill_bar + max_hold_bars, len(candles))

    for i in range(fill_bar, end_bar):
        c = candles[i]
        sl_breach = (c["low"] <= sl_price) if is_buy else (c["high"] >= sl_price)
        if sl_breach and remaining_lot > 1e-9:
            return _close_trade(trade, sl_price, i, fill_bar, is_buy, remaining_lot,
                                 partial_pnl, "sl" if not any(tp_done) else "sl_after_partial")

        for ti, tpv in enumerate(tps):
            if tp_done[ti]:
                continue
            hit = (c["high"] >= tpv) if is_buy else (c["low"] <= tpv)
            if not hit:
                continue
            tp_done[ti]  = True
            move         = (tpv - fill_price) if is_buy else (fill_price - tpv)
            frac         = fracs[ti] if ti < len(fracs) else 0.0
            close_lot    = round(min(lot * frac, remaining_lot), 4)
            if close_lot > 0:
                partial_pnl  += move * close_lot * _USD_PER_PT_PER_LOT
                remaining_lot = round(remaining_lot - close_lot, 4)
            # Trail SL: BE at be_at_pos, then to the previous TP price after each subsequent TP.
            if ti == be_at_pos:
                new_sl = fill_price
            elif ti > be_at_pos:
                new_sl = tps[ti - 1]
            else:
                new_sl = None
            if new_sl is not None:
                if (is_buy and new_sl > sl_price) or (not is_buy and new_sl < sl_price):
                    sl_price = new_sl

        if remaining_lot <= 1e-9:
            trade.close_price   = c["close"]
            trade.close_bar_idx = i
            trade.hold_bars     = i - fill_bar
            trade.pnl_pts       = 0.0
            trade.pnl_usd       = partial_pnl
            trade.outcome       = "all_closed"
            return trade

    # Timed out — close remainder at last close.
    last_close = candles[end_bar - 1]["close"]
    move = (last_close - fill_price) if is_buy else (fill_price - last_close)
    trade.close_price   = last_close
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = move
    trade.pnl_usd       = partial_pnl + move * remaining_lot * _USD_PER_PT_PER_LOT
    trade.outcome       = "timeout"
    return trade


def _simulate_reversal_runner(
    candles: list[dict], sig: BtSignal, fill_bar: int, fill_price: float,
    is_buy: bool, balance: float, risk_pct: float, fixed_lots: float = 0.0,
) -> BtTrade:
    """
    Reversal Runner: keeps the signal's own TP ladder, widens only the SL.

    SL = min(4x stated SL distance, 20pt), floored at 8pt if the stated
    distance is missing/bad data. Back-loaded ladder (5/5/10/10/15/15/15/25%
    for an 8-TP signal, rescaled per _GDVR_PCTS for shorter ladders). SL
    trails to breakeven after TP1, then to the previous TP price after every
    subsequent TP.
    """
    stated_dist = abs(fill_price - sig.stop_loss) if sig.stop_loss else 0.0
    if not stated_dist or stated_dist < 0.5 or stated_dist > 50:
        sl_dist = _GDVR_SL_FLOOR_PT
    else:
        sl_dist = min(stated_dist * _GDVR_SL_MULT, _GDVR_SL_CAP_PT)
    lot = _lot_size(balance, sl_dist, risk_pct, fixed_lots)
    # be_at_pos=1: BE at TP2, matching engine.py's live _handle_reversal_runner
    # docstring ("moving to BE that early [at TP1] would defeat" the wider
    # SL's purpose). The previous version of this simulator moved to BE at
    # TP1 (ti==0) — a real discrepancy from the documented live behaviour,
    # fixed here alongside the ladder-count bug.
    return _run_ladder_strategy(
        candles, sig, fill_bar, fill_price, is_buy, sl_dist, lot,
        "reversal_runner", _GDVR_PCTS, be_at_pos=1, max_hold_bars=_GDVR_MAX_HOLD_BARS,
    )


def _simulate_signal_climber(
    candles: list[dict], sig: BtSignal, fill_bar: int, fill_price: float,
    is_buy: bool, balance: float, risk_pct: float, fixed_lots: float = 0.0,
) -> BtTrade:
    """
    Signal Climber: rides the signal's own SL and TP ladder exactly as sent —
    no SL widening. Front-loaded ladder (_CLIMBER_PCTS), SL to breakeven at
    TP1, then trails to the previous TP price after every subsequent TP.
    """
    sl_dist = abs(fill_price - sig.stop_loss) if sig.stop_loss else 0.0
    if not sl_dist or sl_dist < 0.5 or sl_dist > 50:
        sl_dist = _GDVR_SL_FLOOR_PT  # same bad-data fallback as Reversal Runner
    lot = _lot_size(balance, sl_dist, risk_pct, fixed_lots)
    return _run_ladder_strategy(
        candles, sig, fill_bar, fill_price, is_buy, sl_dist, lot,
        "signal_climber", _CLIMBER_PCTS, be_at_pos=0, max_hold_bars=_GDVR_MAX_HOLD_BARS,
    )


def _simulate_adaptive_runner(
    candles: list[dict], sig: BtSignal, fill_bar: int, fill_price: float,
    is_buy: bool, balance: float, risk_pct: float, fixed_lots: float = 0.0,
) -> BtTrade:
    """
    Adaptive Runner: same widened-SL idea as Reversal Runner, but the widened
    distance is capped at _ADAPTIVE_TP_CAP_FRAC (50%) of the distance to the
    signal's own final (furthest) TP — never wider than that, and never
    tightened below the signal's own stated SL. See
    STRATEGY_DESCRIPTIONS[STRATEGY_ADAPTIVE_RUNNER] in core/models.py for why:
    Reversal Runner's fixed 4x/20pt widening was tuned for Gold Diggers VIP's
    own signal shape (~4-5pt stated SL, ~25-30pt final target) and produces a
    stop wider than the maximum reachable win on shorter-ladder signals
    (e.g. a 3-TP Breakout signal with an 8pt stated SL and a ~15pt final TP).
    """
    stated_dist = abs(fill_price - sig.stop_loss) if sig.stop_loss else 0.0
    if not stated_dist or stated_dist < 0.5 or stated_dist > 50:
        sl_dist = _ADAPTIVE_SL_FLOOR_PT
    else:
        widened = min(stated_dist * _ADAPTIVE_SL_MULT, _ADAPTIVE_SL_CAP_PT)
        # Same correct-side filter as _run_ladder_strategy — a corrupt TP
        # value must not be allowed to set the reachable-reward cap either.
        tps = [
            t for t in sig.tps if t is not None
            and ((is_buy and t > fill_price) or (not is_buy and t < fill_price))
        ]
        final_tp_dist = max((abs(t - fill_price) for t in tps), default=0.0)
        if final_tp_dist > 0:
            tp_cap  = final_tp_dist * _ADAPTIVE_TP_CAP_FRAC
            sl_dist = max(stated_dist, min(widened, tp_cap))
        else:
            sl_dist = widened
    lot = _lot_size(balance, sl_dist, risk_pct, fixed_lots)
    return _run_ladder_strategy(
        candles, sig, fill_bar, fill_price, is_buy, sl_dist, lot,
        "adaptive_runner", _GDVR_PCTS, be_at_pos=0, max_hold_bars=_ADAPTIVE_MAX_HOLD_BARS,
    )


