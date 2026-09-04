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
    # Why this walk refused the template, or "" when it did not. Without it,
    # a refusal reaches the comparison table as zeros -- and zeros read as a
    # strategy that traded nothing and lost nothing, which beside a row
    # showing a real drawdown is an argument FOR the template that could not
    # be simulated at all. See template_simulator.unsupported_reason.
    unsupported_reason: str = ""


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



# ── EA templates ──────────────────────────────────────────────────────────────

TEMPLATE_PREFIX = "template:"


def _load_backtest_template(name: str):
    """The stored EA template called `name`, or None.

    Its own function so the dispatch can be tested without a database, and so
    a missing template is a None rather than an exception on a page render.
    """
    try:
        from backend.src.services.broker.ea_templates import get_ea_template
        return get_ea_template(name)
    except Exception:
        return None


def _simulate_template(
    candles: list, sig: "BtSignal", strategy: str, fill_bar: int,
    fill_price: float, is_buy: bool, balance: float,
) -> "Optional[BtTrade]":
    """Walk an EA template, or return None if it cannot be simulated faithfully.

    None, never an approximation: the picker should not have offered an
    unsupported template, and if it does, this is the backstop. A plausible
    number from a template the walk cannot model is worse than no number,
    because it would be used to choose what trades real money.
    """
    from backend.src.services.backtest.template_simulator import (
        UnsupportedTemplate, simulate as _walk,
    )

    template = _load_backtest_template(strategy[len(TEMPLATE_PREFIX):])
    if not template:
        return None
    try:
        res = _walk(template, candles[fill_bar:], fill_price, is_buy, balance)
    except UnsupportedTemplate:
        return None

    return BtTrade(
        signal_id=sig.signal_id,
        strategy=strategy,
        direction=sig.direction,
        fill_price=fill_price,
        fill_bar_idx=fill_bar,
        lot_size=res.lot_size,
        pnl_pts=round(res.pnl_price, 2),
        pnl_usd=res.pnl_usd,
        close_price=res.close_price,
        # The walk starts at the fill, so its indices are offsets into that
        # slice; the results table shows the index into the whole series.
        close_bar_idx=fill_bar + res.close_bar,
        outcome=res.outcome,
        hold_bars=res.close_bar,
    )

def _simulate_ticks_template(
    ticks: list, sig: "BtSignal", strategy: str, fill_idx: int,
    fill_price: float, is_buy: bool, balance: float,
) -> "Optional[BtTrade]":
    """Tick-walk counterpart to _simulate_template -- same backstop: None,
    never an approximation, for anything the walk cannot model faithfully."""
    from backend.src.services.backtest.template_simulator import (
        UnsupportedTemplate, simulate_ticks as _walk,
    )

    template = _load_backtest_template(strategy[len(TEMPLATE_PREFIX):])
    if not template:
        return None
    try:
        res = _walk(template, ticks[fill_idx:], fill_price, is_buy, balance)
    except UnsupportedTemplate:
        return None

    return BtTrade(
        signal_id=sig.signal_id,
        strategy=strategy,
        direction=sig.direction,
        fill_price=fill_price,
        fill_bar_idx=fill_idx,
        lot_size=res.lot_size,
        pnl_pts=round(res.pnl_price, 2),
        pnl_usd=res.pnl_usd,
        close_price=res.close_price,
        close_bar_idx=fill_idx + res.close_bar,
        outcome=res.outcome,
        hold_bars=res.close_bar,
    )


def _simulate_ticks(
    ticks:              list[dict],
    sig:                BtSignal,
    strategy:           str,
    balance:            float,
    spread_pts:         float,
    commission_per_lot: float = 0.0,
) -> Optional[BtTrade]:
    """Tick-walk counterpart to _simulate() -- template strategies only. The
    backtest picker (frontend/pages/backtest.py) has offered EA templates
    exclusively since item 7, so a built-in strategy key reaching here would
    already be a picker bug; this returns None for one rather than guessing
    which built-in simulator to run against ticks none of them support.
    """
    if not strategy.startswith(TEMPLATE_PREFIX):
        return None

    if sig.direction == "BUY"  and sig.stop_loss >= sig.entry_low:
        return None
    if sig.direction == "SELL" and sig.stop_loss <= sig.entry_high:
        return None

    # No _BROKER_TZ_OFFSET here, unlike _simulate()'s candle path. Candle
    # timestamps come from copy_rates_from_pos, which returns raw MT5 server
    # time with no conversion at all (_get_candles_range's own docstring:
    # copy_rates_range silently misinterprets a naive datetime). Ticks come
    # from copy_ticks_range given a tz-AWARE UTC datetime, and the 2026-09-03
    # probe confirmed the ticks that come back land exactly in the UTC window
    # requested -- already true UTC, same convention sig.created_ts is in.
    # Adding the offset here would silently shift every tick fill by 3 hours.
    fill_idx = None
    for i, t in enumerate(ticks):
        if t["time"] < sig.created_ts:
            continue
        mid = (float(t["bid"]) + float(t["ask"])) / 2.0
        if sig.entry_low <= mid <= sig.entry_high:
            fill_idx = i
            break

    if fill_idx is None:
        return None

    half_spread = spread_pts / 2.0
    is_buy      = sig.direction == "BUY"
    fill_price  = sig.entry_mid + half_spread if is_buy else sig.entry_mid - half_spread

    trade = _simulate_ticks_template(ticks, sig, strategy, fill_idx, fill_price, is_buy, balance)

    if trade is not None and commission_per_lot > 0:
        comm             = round(commission_per_lot * trade.lot_size, 2)
        trade.commission = comm
        trade.pnl_usd    = round(trade.pnl_usd - comm, 2)

    return trade


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
    # Lazy import: simulators.py imports this module's dataclasses/helpers at
    # load time, so importing it at engine.py's top would be a cycle.
    from backend.src.services.backtest.simulators import (
        _run_ladder_strategy,
        _simulate_adaptive_runner, _simulate_be_runner, _simulate_conservative,
        _simulate_ct, _simulate_nss, _simulate_protected_scale,
        _simulate_reversal_runner, _simulate_scale_out, _simulate_signal_climber,
        _simulate_trail_stop,
    )

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

    if strategy.startswith(TEMPLATE_PREFIX):
        trade = _simulate_template(
            candles, sig, strategy, fill_bar, fill_price, is_buy, balance)
    elif strategy == "conservative":
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

        stats = _compute_stats(strategy, trades, starting_balance)
        stats.unsupported_reason = _template_refusal(strategy, tick_mode=False)
        out[strategy] = stats

    return out


def run_backtest_ticks(
    signals:            list[BtSignal],
    ticks:               list[dict],
    strategies:          list[str],
    starting_balance:    float = 1_000.0,
    spread_pts:          float = 0.4,
    commission_per_lot:  float = 7.0,
) -> dict[str, StrategyStats]:
    """Tick-walk counterpart to run_backtest() -- docs/todo/backtest/010
    phase 1. EA templates only; a non-template strategy key produces no
    trades rather than a guess at which built-in simulator to run on ticks.
    """
    out: dict[str, StrategyStats] = {}

    for strategy in strategies:
        trades:          list[BtTrade] = []
        current_balance: float         = starting_balance

        for sig in signals:
            t = _simulate_ticks(ticks, sig, strategy, current_balance,
                                spread_pts, commission_per_lot)
            if t is not None:
                trades.append(t)
                current_balance = max(current_balance + t.pnl_usd, 1.0)

        stats = _compute_stats(strategy, trades, starting_balance)
        stats.unsupported_reason = _template_refusal(strategy, tick_mode=True)
        out[strategy] = stats

    return out


def _template_refusal(strategy: str, tick_mode: bool) -> str:
    """Why this walk refused `strategy`'s template, or "" when it did not.

    Deliberately narrow. A built-in strategy is not "unsupported" -- it is
    simply not walked on ticks, a different silence. A template that no longer
    exists is a different problem again, and labelling it unsupported sends
    the user to edit a trail mode on a template that is not there. Only a
    template the walk actively refuses gets a reason.
    """
    if not strategy.startswith(TEMPLATE_PREFIX):
        return ""
    template = _load_backtest_template(strategy[len(TEMPLATE_PREFIX):])
    if not template:
        return ""
    try:
        from backend.src.services.backtest.template_simulator import (
            unsupported_reason as _why,
        )
        return _why(template, tick_mode)
    except Exception:
        return ""


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
    from backend.src.services.backtest import repo as backtest_repo
    results = []
    try:
        rows = backtest_repo.fetch_backtest_signals(live_trades_only)
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
