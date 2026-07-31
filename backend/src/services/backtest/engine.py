"""
Backtest engine for XAUUSD strategy simulation.

Key design principles (matching professional FOREX backtesting practice):
  - Signals are tested only FORWARD from their creation time; no candles before
    the signal's creation timestamp are used for fill detection.
  - Signals are pre-filtered to the loaded candle window so old signals at
    different price levels are not inadvertently "filled" against today's data.
  - Corrupt signals (SL=0, point-entry zones, absurd SL distances) are rejected
    before they can skew lot sizes to minimum and generate phantom losses.
  - Lot size is re-calculated on CURRENT account equity after every trade
    (not fixed at starting balance) so compounding and drawdown are realistic.
  - Commission is deducted from every trade P&L.

XAUUSD contract: 1 lot = 100 oz; a $1 price move = $100/lot.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

_USD_PER_PT_PER_LOT  = 100.0
_MAX_HOLD_BARS       = 96        # ~8h on M5, ~24h on M15
_MIN_LOT             = 0.01
_MAX_LOT             = 5.0
_BROKER_TZ_OFFSET    = 10_800   # Vantage MT5 candle ts is UTC+3; signal ts is UTC

_TRAIL_DIST_PTS      = 5.0      # trailing stop (matches live engine default)

# Reversal Runner constants (must stay in sync with engine.py's _GDVR_* values).
# Validated against 259 Gold Diggers VIP signals (May-Jun 2026): baseline
# (stated SL, full close @ TP1) averages -0.127R/trade; this ladder averages
# +0.167R/trade at 88.7% win rate. See STRATEGY_DESCRIPTIONS[STRATEGY_REVERSAL_RUNNER].
_GDVR_SL_MULT        = 4.0
_GDVR_SL_CAP_PT       = 20.0
_GDVR_SL_FLOOR_PT     = 8.0
_GDVR_MAX_HOLD_BARS   = 288       # ~24h on M5 — matches the 1440min MAX_HOLD validated in research

# Ladder fractions indexed by ACTUAL TP count (1-8), mirroring engine.py's
# _GDVR_PCTS / _CLIMBER_PCTS dicts exactly. The previous _GDVR_LADDER here was
# a flat 8-slot list applied by TP *position* regardless of how many TPs a
# signal actually carried — for a 3-TP signal only ti=0,1,2 ever fired
# (fractions 0.05+0.05+0.10 = 0.20 of the lot), silently leaving 80% of the
# position open with no TP left to close it, exposed only to the (widened) SL
# or the 24h timeout. That understated every non-8-TP signal's real P&L and
# made Reversal Runner look far worse than engine.py's live version (which was
# already count-aware) would actually perform. Confirmed 2026-07-15 via a
# side-by-side backtest against 730 live-executed signals: 313 of them
# (Breakout/Bounce/Signal Generator sources) carry only 3 TPs.
_GDVR_PCTS: dict[int, list[float]] = {
    1: [1.00],
    2: [0.30, 0.70],
    3: [0.15, 0.25, 0.60],
    4: [0.10, 0.15, 0.25, 0.50],
    5: [0.10, 0.10, 0.15, 0.25, 0.40],
    6: [0.08, 0.08, 0.12, 0.17, 0.20, 0.35],
    7: [0.07, 0.07, 0.10, 0.13, 0.13, 0.20, 0.30],
    8: [0.05, 0.05, 0.10, 0.10, 0.15, 0.15, 0.15, 0.25],
}

# Signal Climber ladder (mirrors engine.py's _CLIMBER_PCTS). Front-loaded vs
# Reversal Runner's back-loaded shape — BE at TP1 instead of TP2.
_CLIMBER_PCTS: dict[int, list[float]] = {
    1: [1.00],
    2: [0.40, 0.60],
    3: [0.30, 0.30, 0.40],
    4: [0.20, 0.25, 0.25, 0.30],
    5: [0.20, 0.15, 0.15, 0.20, 0.30],
    6: [0.20, 0.15, 0.15, 0.15, 0.20, 0.15],
    7: [0.20, 0.10, 0.10, 0.15, 0.15, 0.20, 0.10],
    8: [0.20, 0.10, 0.10, 0.10, 0.15, 0.15, 0.10, 0.10],
}

# Adaptive Runner — same widened-SL idea as Reversal Runner, but the widened
# distance is additionally capped at a fraction of the signal's own final
# (furthest) TP, so the stop can never end up wider than — or too close to —
# the maximum reachable reward. Never tightened below the signal's own
# stated SL (if there's no room to widen, the stated SL is used unchanged
# rather than forcing extra "room to breathe" that isn't structurally there).
# See STRATEGY_DESCRIPTIONS[STRATEGY_ADAPTIVE_RUNNER] in core/models.py.
_ADAPTIVE_SL_MULT       = 4.0
_ADAPTIVE_SL_CAP_PT      = 20.0
_ADAPTIVE_SL_FLOOR_PT    = 8.0
_ADAPTIVE_TP_CAP_FRAC    = 0.50   # widened SL capped at this fraction of final-TP distance
_ADAPTIVE_MAX_HOLD_BARS  = 288


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class BtSignal:
    signal_id:  str
    direction:  str            # "BUY" | "SELL"
    entry_low:  float
    entry_high: float
    stop_loss:  float
    tp1:        Optional[float]
    tp2:        Optional[float]
    tp3:        Optional[float]
    created_ts: float          # unix epoch UTC
    source:     str = "manual"
    tp4:        Optional[float] = None
    tp5:        Optional[float] = None
    tp6:        Optional[float] = None
    tp7:        Optional[float] = None
    tp8:        Optional[float] = None

    @property
    def tps(self) -> list[Optional[float]]:
        return [self.tp1, self.tp2, self.tp3, self.tp4, self.tp5, self.tp6, self.tp7, self.tp8]

    @property
    def entry_mid(self) -> float:
        return (self.entry_low + self.entry_high) / 2


@dataclass
class BtTrade:
    signal_id:     str
    strategy:      str
    direction:     str
    fill_price:    float
    fill_bar_idx:  int
    lot_size:      float
    pnl_pts:       float = 0.0
    pnl_usd:       float = 0.0
    commission:    float = 0.0
    close_price:   float = 0.0
    close_bar_idx: int   = 0
    outcome:       str   = ""   # "sl"|"tp1_only"|"tp1_tp3"|"tp3_direct"|"timeout"
    hold_bars:     int   = 0


@dataclass
class StrategyStats:
    strategy:         str
    trades:           int
    wins:             int
    losses:           int
    win_rate:         float
    total_pnl:        float
    total_commission: float
    avg_win:          float
    avg_loss:         float
    profit_factor:    float
    max_drawdown_pct: float
    sharpe:           float
    final_balance:    float
    equity_curve:     list[float] = field(default_factory=list)
    trade_list:       list[BtTrade] = field(default_factory=list)


# ── Signal pre-filter ─────────────────────────────────────────────────────────

@dataclass
class FilterStats:
    total:          int = 0
    valid:          int = 0
    out_of_window:  int = 0
    zero_sl:        int = 0
    point_entry:    int = 0
    wide_sl:        int = 0
    bad_tp:         int = 0
    candle_start:   Optional[str] = None   # UTC datetime string
    candle_end:     Optional[str] = None
    signal_start:   Optional[str] = None
    signal_end:     Optional[str] = None


def filter_signals(
    signals:      list[BtSignal],
    candles:      list[dict],
    max_sl_pts:   float = 50.0,
) -> tuple[list[BtSignal], FilterStats]:
    """
    Filter signals to those valid for backtesting against the loaded candle data.

    Rejected signals (each counted separately):
      out_of_window — signal created_ts is outside the candle window (UTC)
      zero_sl       — stop_loss is 0 (instant order; no protective level defined)
      point_entry   — entry_low == entry_high (no zone; typically a market order)
      wide_sl       — SL distance from entry_mid exceeds max_sl_pts
      bad_tp        — a stored TP is on the wrong side of entry_mid for the
                       signal's direction (e.g. a BUY's tp2 below entry) — the
                       same corrupt/truncated-price failure mode the SL check
                       above catches, applied to TPs. Second line of defense:
                       simulators also re-check each TP against the actual
                       fill price before treating a touch as a hit.

    Returns (valid_signals, FilterStats).
    """
    stats = FilterStats(total=len(signals))
    if not signals:
        return [], stats

    if candles:
        # Candle timestamps are UTC+3; convert to UTC for comparison with signal ts
        c_start_utc = candles[0]["ts"]  - _BROKER_TZ_OFFSET
        c_end_utc   = candles[-1]["ts"] - _BROKER_TZ_OFFSET
        fmt = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        stats.candle_start = fmt(c_start_utc)
        stats.candle_end   = fmt(c_end_utc)
    else:
        c_start_utc = 0.0
        c_end_utc   = float("inf")

    all_ts = [s.created_ts for s in signals]
    stats.signal_start = datetime.fromtimestamp(min(all_ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    stats.signal_end   = datetime.fromtimestamp(max(all_ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # 8-hour grace: allow signals created up to 8h BEFORE the candle window opens.
    # Covers Asian signals tested against London candles, or signals created a few
    # minutes before the first fetched candle. Signals older than window-start − 8h
    # would have no candles to fill against (the fill loop skips earlier bars).
    _GRACE_SECS = 8 * 3600

    valid = []
    for s in signals:
        # 1. Time window: signal must not predate window-start by more than 8 hours,
        #    and must not be from after the candle window closed.
        if s.created_ts < (c_start_utc - _GRACE_SECS) or s.created_ts > c_end_utc:
            stats.out_of_window += 1
            continue

        # 2. Zero / missing SL (instant market orders parsed without a stop level)
        if not s.stop_loss or s.stop_loss <= 0:
            stats.zero_sl += 1
            continue

        # 3. Point entry (no zone — probably a market order with a fixed entry price)
        if abs(s.entry_high - s.entry_low) < 0.01:
            stats.point_entry += 1
            continue

        # 4. Sanity-check SL distance (catches parsing errors like SL=4 when it
        #    should be 4244, or entry_high=41303 instead of 4130)
        sl_dist = abs(s.entry_mid - s.stop_loss)
        if sl_dist > max_sl_pts:
            stats.wide_sl += 1
            continue

        # 5. Sanity-check TP side (catches the same corrupt/truncated-price
        #    parsing errors as #4, but on the take-profit side — e.g. a
        #    stored tp2 of 40.0 for a BUY at entry ~4048).
        is_buy = s.direction == "BUY"
        if any(
            t is not None and not ((is_buy and t > s.entry_mid) or (not is_buy and t < s.entry_mid))
            for t in s.tps
        ):
            stats.bad_tp += 1
            continue

        valid.append(s)

    stats.valid = len(valid)
    return valid, stats


# ── Math helpers ──────────────────────────────────────────────────────────────

def _lot_size(balance: float, sl_dist: float, risk_pct: float, fixed_lots: float = 0.0) -> float:
    """Lot size from risk %. If fixed_lots > 0 use it directly."""
    if fixed_lots > 0:
        return round(min(_MAX_LOT, max(_MIN_LOT, fixed_lots)), 2)
    if sl_dist <= 0:
        return _MIN_LOT
    risk_usd = balance * risk_pct / 100.0
    return round(min(_MAX_LOT, max(_MIN_LOT, risk_usd / (sl_dist * _USD_PER_PT_PER_LOT))), 2)


def _close_trade(
    trade: BtTrade, close_px: float, bar_idx: int, fill_bar: int,
    is_buy: bool, remaining_lot: float, partial_pnl: float, outcome: str,
) -> BtTrade:
    move = (close_px - trade.fill_price) if is_buy else (trade.fill_price - close_px)
    trade.close_price   = close_px
    trade.close_bar_idx = bar_idx
    trade.hold_bars     = bar_idx - fill_bar
    trade.pnl_pts       = move
    trade.pnl_usd       = partial_pnl + move * remaining_lot * _USD_PER_PT_PER_LOT
    trade.outcome       = outcome
    return trade


def _valid_tp(tp: Optional[float], is_buy: bool, fill_price: float) -> Optional[float]:
    """
    Reject a TP that's on the wrong side of the fill price — same side-check as
    engine.py's live _run_tp_ladder and _run_ladder_strategy above. A corrupt/
    truncated TP value (e.g. tp2=40.0 for a BUY filled at ~4048) would otherwise
    read as "instantly hit" on the very first candle.
    """
    if tp is None:
        return None
    if (is_buy and tp > fill_price) or (not is_buy and tp < fill_price):
        return tp
    return None


def _atr14(candles: list[dict], end_idx: int) -> float:
    start  = max(0, end_idx - 14)
    window = candles[start:end_idx + 1]
    if len(window) < 2:
        return 1.0
    trs = []
    for i in range(1, len(window)):
        h, l, pc = window[i]["high"], window[i]["low"], window[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 1.0


def _ema_closes(candles: list[dict], end_idx: int, period: int) -> Optional[float]:
    """EMA of close prices. Same algorithm as engine.py's _ema()."""
    start  = max(0, end_idx - period * 3)
    closes = [c["close"] for c in candles[start:end_idx + 1]]
    if len(closes) < period:
        return None
    k   = 2.0 / (period + 1.0)
    ema = closes[0]
    for v in closes[1:]:
        ema = v * k + ema * (1.0 - k)
    return ema


# ── Strategy simulators ───────────────────────────────────────────────────────

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

    trade.close_price   = fill_price
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = 0.0
    trade.pnl_usd       = partial_pnl
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

    trade.close_price   = fill_price
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = 0.0
    trade.pnl_usd       = partial_pnl
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

    trade.close_price   = fill_price
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = 0.0
    trade.pnl_usd       = partial_pnl
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

    trade.close_price   = fill_price
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = 0.0
    trade.pnl_usd       = 0.0
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

    trade.close_price   = fill_price
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = 0.0
    trade.pnl_usd       = partial_pnl
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

    trade.close_price   = fill_price
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = 0.0
    trade.pnl_usd       = partial_pnl
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

    trade.close_price   = fill_price
    trade.close_bar_idx = end_bar - 1
    trade.hold_bars     = end_bar - 1 - fill_bar
    trade.pnl_pts       = 0.0
    trade.pnl_usd       = 0.0
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


# ── Main simulation dispatcher ────────────────────────────────────────────────

def _simulate(
    candles:           list[dict],
    sig:               BtSignal,
    strategy:          str,
    balance:           float,
    risk_pct:          float,
    spread_pts:        float,
    fixed_lots:        float = 0.0,
    commission_per_lot: float = 0.0,
) -> Optional[BtTrade]:
    """Simulate one signal under one strategy. Returns None if signal doesn't fill."""

    if strategy not in ("conservative", "conservative_trial"):
        if sig.direction == "BUY"  and sig.stop_loss >= sig.entry_low:
            return None
        if sig.direction == "SELL" and sig.stop_loss <= sig.entry_high:
            return None

    # Signals were pre-filtered to the candle window by filter_signals().
    # The ts check below is a safety net: skip candles before the signal's creation.
    sig_broker_ts = sig.created_ts + _BROKER_TZ_OFFSET

    fill_bar = None
    for i, c in enumerate(candles):
        if c["ts"] < sig_broker_ts:
            continue
        if sig.direction == "BUY"  and c["low"]  <= sig.entry_high and c["high"] >= sig.entry_low:
            fill_bar = i; break
        if sig.direction == "SELL" and c["high"] >= sig.entry_low  and c["low"]  <= sig.entry_high:
            fill_bar = i; break

    if fill_bar is None:
        return None

    half_spread = spread_pts / 2.0
    is_buy      = sig.direction == "BUY"
    fill_price  = sig.entry_mid + half_spread if is_buy else sig.entry_mid - half_spread
    sl_dist     = abs(fill_price - sig.stop_loss)

    if strategy == "conservative":
        trade = _simulate_conservative(candles, sig, fill_bar, fill_price, is_buy, balance, risk_pct, fixed_lots)
    elif strategy == "conservative_trial":
        trade = _simulate_ct(candles, sig, fill_bar, fill_price, is_buy, balance, risk_pct, fixed_lots)
    elif strategy == "no_sl_scale":
        trade = _simulate_nss(candles, sig, fill_bar, fill_price, is_buy, sl_dist, balance, risk_pct, fixed_lots)
    elif strategy == "be_runner":
        trade = _simulate_be_runner(candles, sig, fill_bar, fill_price, is_buy, sl_dist, balance, risk_pct, fixed_lots)
    elif strategy == "scale_out":
        trade = _simulate_scale_out(candles, sig, fill_bar, fill_price, is_buy, sl_dist, balance, risk_pct, fixed_lots)
    elif strategy == "protected_scale":
        trade = _simulate_protected_scale(candles, sig, fill_bar, fill_price, is_buy, sl_dist, balance, risk_pct, fixed_lots)
    elif strategy == "trail_stop":
        trade = _simulate_trail_stop(candles, sig, fill_bar, fill_price, is_buy, sl_dist, balance, risk_pct, fixed_lots)
    elif strategy == "reversal_runner":
        trade = _simulate_reversal_runner(candles, sig, fill_bar, fill_price, is_buy, balance, risk_pct, fixed_lots)
    elif strategy == "signal_climber":
        trade = _simulate_signal_climber(candles, sig, fill_bar, fill_price, is_buy, balance, risk_pct, fixed_lots)
    elif strategy == "adaptive_runner":
        trade = _simulate_adaptive_runner(candles, sig, fill_bar, fill_price, is_buy, balance, risk_pct, fixed_lots)
    else:
        return None

    # Apply round-turn commission after strategy simulator sets pnl_usd
    if trade is not None and commission_per_lot > 0:
        comm           = round(commission_per_lot * trade.lot_size, 2)
        trade.commission = comm
        trade.pnl_usd    = round(trade.pnl_usd - comm, 2)

    return trade


# ── Public API ────────────────────────────────────────────────────────────────

def run_backtest(
    signals:            list[BtSignal],
    candles:            list[dict],
    strategies:         list[str],
    starting_balance:   float = 1_000.0,
    risk_pct:           float = 1.0,
    spread_pts:         float = 0.4,
    lots_per_trade:     float = 0.0,
    commission_per_lot: float = 7.0,
) -> dict[str, StrategyStats]:
    """
    Simulate every signal under every requested strategy.
    Each strategy gets an independent account; results are not cross-contaminated.
    Lot size is calculated on current account equity after every trade (not fixed).
    Commission is deducted from every trade P&L.
    """
    out: dict[str, StrategyStats] = {}

    for strategy in strategies:
        trades:          list[BtTrade] = []
        current_balance: float         = starting_balance

        for sig in signals:
            t = _simulate(candles, sig, strategy, current_balance, risk_pct,
                          spread_pts, lots_per_trade, commission_per_lot)
            if t is not None:
                trades.append(t)
                current_balance = max(current_balance + t.pnl_usd, 1.0)

        out[strategy] = _compute_stats(strategy, trades, starting_balance)

    return out


def _compute_stats(
    strategy: str, trades: list[BtTrade], starting_balance: float
) -> StrategyStats:
    wins   = [t for t in trades if t.pnl_usd > 0]
    losses = [t for t in trades if t.pnl_usd <= 0]

    bal    = starting_balance
    eq     = [round(bal, 2)]
    peak   = bal
    max_dd = 0.0
    for t in trades:
        bal   += t.pnl_usd
        eq.append(round(bal, 2))
        peak   = max(peak, bal)
        dd     = (peak - bal) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    total_pnl    = sum(t.pnl_usd for t in trades)
    total_comm   = sum(t.commission for t in trades)
    avg_win      = sum(t.pnl_usd for t in wins)  / len(wins)   if wins   else 0.0
    avg_loss     = sum(t.pnl_usd for t in losses) / len(losses) if losses else 0.0
    gross_wins   = sum(t.pnl_usd for t in wins)
    gross_losses = abs(sum(t.pnl_usd for t in losses))
    pf           = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    returns = [t.pnl_usd for t in trades]
    if len(returns) >= 2:
        avg_r  = sum(returns) / len(returns)
        std_r  = math.sqrt(sum((r - avg_r) ** 2 for r in returns) / len(returns))
        sharpe = avg_r / std_r if std_r > 0 else 0.0
    else:
        sharpe = 0.0

    return StrategyStats(
        strategy         = strategy,
        trades           = len(trades),
        wins             = len(wins),
        losses           = len(losses),
        win_rate         = len(wins) / len(trades) if trades else 0.0,
        total_pnl        = total_pnl,
        total_commission = total_comm,
        avg_win          = avg_win,
        avg_loss         = avg_loss,
        profit_factor    = pf,
        max_drawdown_pct = max_dd * 100,
        sharpe           = sharpe,
        final_balance    = starting_balance + total_pnl,
        equity_curve     = eq,
        trade_list       = trades,
    )


def signals_from_db(live_trades_only: bool = False) -> list[BtSignal]:
    """
    Load signals from vantage_signals.
    live_trades_only=True: signals that resulted in an actual MT5 trade
    (Breakout Engine, Signal Generator/bounce, Telegram channels).
    """
    from backend.src.db import database as db
    results = []
    try:
        with db.db() as conn:
            if live_trades_only:
                rows = conn.execute(
                    "SELECT vs.signal_id, vs.direction, vs.entry_low, vs.entry_high, "
                    "vs.stop_loss, vs.tp1, vs.tp2, vs.tp3, vs.created_at, vs.source_name, "
                    "vs.tp4, vs.tp5, vs.tp6, vs.tp7, vs.tp8 "
                    "FROM vantage_signals vs "
                    "INNER JOIN vantage_simulated_trades vst ON vst.signal_id = vs.signal_id "
                    "GROUP BY vs.signal_id ORDER BY vs.created_at"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT signal_id, direction, entry_low, entry_high, stop_loss, "
                    "tp1, tp2, tp3, created_at, source_name, tp4, tp5, tp6, tp7, tp8 "
                    "FROM vantage_signals ORDER BY created_at"
                ).fetchall()
        for r in rows:
            results.append(BtSignal(
                signal_id  = str(r[0]),
                direction  = r[1],
                entry_low  = float(r[2]),
                entry_high = float(r[3]),
                stop_loss  = float(r[4]) if r[4] else 0.0,
                tp1        = float(r[5]) if r[5] else None,
                tp2        = float(r[6]) if r[6] else None,
                tp3        = float(r[7]) if r[7] else None,
                created_ts = float(r[8]),
                source     = r[9] or "unknown",
                tp4        = float(r[10]) if r[10] else None,
                tp5        = float(r[11]) if r[11] else None,
                tp6        = float(r[12]) if r[12] else None,
                tp7        = float(r[13]) if r[13] else None,
                tp8        = float(r[14]) if r[14] else None,
            ))
    except Exception:
        pass
    return results
