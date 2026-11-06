"""Simulate an EA-template trade over historical bars.

Option A of docs/todo/backtest/010. Every rule here was read out of
`ManageTemplate()` in ForexTraderBridge.mq5 (lines 2655-2941), not inferred
from field names -- the entire value of this module is that its numbers match
what the EA actually does on a live chart. A plausible guess that diverges is
worse than no backtest, because the numbers get trusted.

Four rules that a reading of the field names alone would have got wrong:

  * `sl_pips`, `tp{n}_pips` and every `trail_*` distance are PIPS, and the EA
    converts with `PipsToPrice(p) = p * 10 * _Point`. On XAUUSD (_Point 0.01)
    that is `p * 0.1` price units. This backtest works in price units, so
    50 pips is a 5.0 move.
  * `be_trigger` is a TP LEVEL INDEX (1-based), not a distance. `be_trigger=2`
    means "move to breakeven once TP2 clears".
  * a partial closes `original_lot * pct`, capped at what remains -- not a
    share of the remainder. `tp{n}_pct` is stored 0-100 and divided by 100
    before it reaches the EA (open_trade.py:217).
  * the ladder walks IN ORDER and breaks at the first level price has not
    reached. A bar that leaps past TP2 still takes TP1 first.

Breakeven LATCHES. The EA arms off `triggered[]`, not a live price test,
because re-asking "is price beyond the TP right now" un-armed the move on any
retrace: 141 trades over a month never reached breakeven and closed a mean
66.7 pips below entry (-$7,562). This walk latches the same way.

WHAT THIS REFUSES rather than approximates: grid mode, resting entries, live
ATR sizing, account-wide harvest and equity protection (see template_support),
plus the `staged` and `fractal` trail modes. No currently supported template
uses either, so refusing costs nothing and cannot silently diverge.

Bar-based, so it inherits the same-bar ambiguity the timeframe descriptions
already admit: when one bar spans both the stop and a target, this resolves
the STOP first. That is the pessimistic choice, and deliberately the opposite
of the existing simulators' optimistic "assume TP1 hit first" -- a template
backtest that flatters itself is the failure mode worth avoiding. Ticks remove
the ambiguity entirely; see phase 1 of the scope.

THE TWO WALKS NO LONGER SHARE EVERY RULE. `simulate_ticks` was brought into
line with MoveSl and ManageTemplate on 2026-11-06 (its docstring lists how).
`simulate` was deliberately left as it was, and still has every one of those
gaps: no guard clamp, TP levels fill at the level, a zero-lot level latches, a
`tp` trail and a breakeven only move on a latched level, and with no
trail_activation the trail never arms. See the engines domain README.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from backend.src.services.backtest.template_support import can_simulate

__all__ = ["simulate", "simulate_ticks", "pips_to_price", "TemplateResult",
           "UnsupportedTemplate", "trail_inside_bars_reason"]

# XAUUSD. The EA's own conversion, kept as one constant so a symbol change is
# one edit rather than a hunt: PipsToPrice(p) = p * 10 * _Point.
_POINT = 0.01
_PIP_IN_PRICE = 10.0 * _POINT

# Matches engine.py: a "point" there is one price unit, $100 per lot.
_USD_PER_PRICE_UNIT_PER_LOT = 100.0
_MIN_LOT = 0.01
_MAX_LOT = 5.0

_MAX_TP_LEVELS = 8

# Trail modes this walk reproduces faithfully. `staged` (a three-rung SL
# ratchet with per-rung remove_tp) and `fractal` (needs fractal detection)
# are refused instead.
_SUPPORTED_TRAIL_MODES = ("off", "", "step", "candle", "tp")

# The tick walk additionally refuses `candle`: CandleTrailLevel() (mql5:2558-
# 2576) trails to the lowest low / highest high of the last 3 CLOSED M15
# candles, fixed regardless of the backtest's own timeframe -- data this walk
# has no access to (it has bid/ask, not an M15 candle series). The bar walk's
# own "candle" mode trails to the previous BAR's low/high instead, which is
# only the same thing when the backtest happens to be run at M15 with a
# 1-candle lookback; at any other timeframe, or with the EA's real 3-candle
# lookback, it diverges from the EA. Tracked as a known bar-walk fidelity gap
# in docs/todo/backtest/010 rather than fixed here or silently inherited.
_SUPPORTED_TICK_TRAIL_MODES = ("off", "", "step", "tp")

# InpDefaultTrailStopPts (default 5.0): the EA's fallback when trail_distance
# is 0, a raw price delta rather than pips. An EA input, so a chart can differ.
_EA_DEFAULT_TRAIL_DISTANCE = 5.0


class UnsupportedTemplate(Exception):
    """This template uses something the walk cannot model. Never approximate."""


@dataclass
class TemplateResult:
    lot_size:       float
    entry:          float
    outcome:        str = "timeout"      # "sl" | "tp" | "timeout"
    close_price:    float = 0.0
    close_bar:      int = 0
    pnl_price:      float = 0.0          # in price units
    pnl_usd:        float = 0.0
    closed_lots:    list = field(default_factory=list)
    remaining_lots: float = 0.0


def pips_to_price(pips: float) -> float:
    """The EA's PipsToPrice, in price units."""
    try:
        return float(pips) * _PIP_IN_PRICE
    except (TypeError, ValueError):
        return 0.0


def _f(template: dict, key: str, default: float = 0.0) -> float:
    try:
        v = template.get(key)
        return float(default if v is None or v == "" else v)
    except (TypeError, ValueError):
        return default


def _ladder(template: dict, entry: float, sign: float,
            atr: float = 0.0) -> list[tuple[float, float]]:
    """[(price, fraction_of_original_lot)] for each defined TP, in order.

    A level counts as defined when its pips are non-zero -- the same test
    `hasTp[]` makes on the EA side.

    With an ATR and `use_dynamic_atr`, level 1 lands on its ATR multiple,
    and with `atr_ladder_scale` the whole ladder is rescaled around it. The
    rule comes from `trading/template_levels`, the same module the live
    path uses: two copies of it is how a backtest ends up measuring a
    ladder the live trade never had.
    """
    from backend.src.services.trading import template_levels as _tl

    out: list[tuple[float, float]] = []
    factor = _tl.atr_scale_factor(template, atr)
    tp1_dist = _tl.atr_tp1_distance(template, atr)
    for n in range(1, _MAX_TP_LEVELS + 1):
        pips = _f(template, f"tp{n}_pips", 0.0)
        if pips <= 0:
            continue
        dist = pips_to_price(pips)
        if factor is not None:
            dist *= factor
        elif n == 1 and tp1_dist is not None:
            dist = tp1_dist
        out.append((entry + sign * dist,
                    _f(template, f"tp{n}_pct", 0.0) / 100.0))
    return out


def _lot_for(template: dict, sl_distance: float, balance: float) -> float:
    """The template's own Anchor Lot, or a risk-derived size when risk_pct>0."""
    risk_pct = _f(template, "risk_pct", 0.0)
    if risk_pct > 0 and sl_distance > 0:
        risk_usd = balance * risk_pct / 100.0
        lot = risk_usd / (sl_distance * _USD_PER_PRICE_UNIT_PER_LOT)
    else:
        lot = _f(template, "lot_anchor", _MIN_LOT)
    return round(min(_MAX_LOT, max(_MIN_LOT, lot)), 2)


def trail_inside_bars_reason(template: dict, bars: list) -> str:
    """Why this trail cannot be walked on THESE bars, or "" when it can.

    A property of the pairing, not of the template, which is why it is asked
    here and not in `can_simulate`: the same template is honest on one series
    and meaningless on a coarser one.

    `simulate` tests the stop using the level it held at the END of the
    previous bar, then advances the trail from THIS bar's high. Inside a
    single bar the trail can therefore move and never be tested, while the
    live EA trails and tests on every tick. The error is invisible while the
    trail is wide and decisive once it is narrow, and it announces itself only
    as an edge that GROWS the coarser the bars get. Measured 2026-09-22 on 261
    real fills, one template, identical entries, only the bar size changing:

        M1  PF 1.22 (+0.26)   M5  1.74 (+1.24)
        M15 1.92 (+2.27)      H1  1.91 (+4.80)

    A real edge does not depend on the resolution it is measured at. That
    template carried a $1.50 trail against median bar ranges of $1.73 (M1)
    through $16.68 (H1) -- unsimulatable on every series available, including
    the finest. It was recommended off the M5 number and lost money live.

    Deliberately NOT enforced inside `simulate`: the trail mechanics are unit
    tested on coarse synthetic bars where the arithmetic is readable, and
    those tests are right. This is for the callers that walk real market data.
    """
    mode = str(template.get("trail_mode") or "off").strip().lower()
    dist = pips_to_price(_f(template, "trail_distance", 0.0))
    if mode in ("off", "") or dist <= 0 or not bars:
        return ""
    ranges = sorted(float(b["high"]) - float(b["low"]) for b in bars)
    median = ranges[len(ranges) // 2]
    if median <= 0 or dist >= median:
        return ""
    return (
        f"trail_distance {dist:.2f} is inside the bars it would be walked on "
        f"(median range {median:.2f}): the stop would move and be hit within "
        f"a single bar, which a bar walk cannot see. The result would report "
        f"an edge that is only the bar size. Use the tick walk, choose a finer "
        f"timeframe, or widen the trail."
    )


def unsupported_reason(template: dict, tick_mode: bool = False,
                       atr_available: bool = False) -> str:
    """Why this walk would refuse `template`, or "" when it would not.

    The same decision _guard makes, asked without running a simulation, so a
    caller can report the refusal instead of aggregating the resulting
    absence of trades into a row of zeros. Zeros are what the owner saw on
    2026-09-04 for a trail_mode=candle template on a tick run, and zeros in a
    comparison table read as "traded nothing and lost nothing" rather than
    "this walk cannot model it".
    """
    try:
        _guard(template, tick_mode, atr_available)
    except UnsupportedTemplate as exc:
        return str(exc)
    return ""


def _guard(template: dict, tick_mode: bool = False,
           atr_available: bool = False) -> None:
    # A tick series has no candles to derive an ATR from, the same argument
    # that makes trail_mode=candle unsimulatable on ticks -- so the tick
    # walk never claims one however the caller asks.
    ok, reasons = can_simulate(template, atr_available and not tick_mode)
    if not ok:
        raise UnsupportedTemplate("; ".join(reasons))
    mode = str(template.get("trail_mode") or "off").strip().lower()
    supported = _SUPPORTED_TICK_TRAIL_MODES if tick_mode else _SUPPORTED_TRAIL_MODES
    if mode not in supported:
        if tick_mode and mode == "candle":
            raise UnsupportedTemplate(
                "trail_mode=candle: not simulated on ticks. The EA trails to "
                "the last 3 closed M15 candles, which this walk has no candle "
                "series to reproduce -- use the bar-based backtest instead."
            )
        raise UnsupportedTemplate(
            f"trail_mode={mode}: not simulated. `staged` is a three-rung SL "
            "ratchet and `fractal` needs fractal detection; neither is "
            "reproduced here, and approximating them would diverge from the "
            "EA silently."
        )


def simulate(template: dict, bars: list, entry: float, is_buy: bool,
             balance: float = 10_000.0, atr: float = 0.0) -> TemplateResult:
    """Walk `bars` under `template`, starting from a fill at `entry`.

    Raises UnsupportedTemplate rather than approximating anything.

    `atr` is the volatility at the fill, in PRICE. When the template has
    `use_dynamic_atr` on, it sizes the stop and the ladder exactly as the
    live path does; at 0.0 the walk falls back to `sl_pips` and refuses a
    dynamic-ATR template outright, which is what it did before.
    """
    _guard(template, atr_available=atr > 0)

    from backend.src.services.trading import template_levels as _tl

    sign = 1.0 if is_buy else -1.0
    sl_distance = (_tl.atr_sl_distance(template, atr)
                   or pips_to_price(_f(template, "sl_pips", 0.0)))
    lot = _lot_for(template, sl_distance, balance)

    stop: Optional[float] = (entry - sign * sl_distance) if sl_distance > 0 else None
    ladder = _ladder(template, entry, sign, atr)
    triggered = [False] * len(ladder)

    ladder_on = str(template.get("tpsl_mode") or "on").strip().lower() != "off"
    partials_on = bool(_f(template, "partials", 1))
    close_full_on_last = bool(_f(template, "close_full_on_last", 0))

    be_level = int(_f(template, "be_trigger", 0))
    be_mode = str(template.get("be_mode") or "entry").strip().lower()
    be_buffer = _f(template, "be_buffer_pts", 0.0)

    trail_mode = str(template.get("trail_mode") or "off").strip().lower()
    trail_dist = pips_to_price(_f(template, "trail_distance", 0.0))
    trail_pad = pips_to_price(_f(template, "trail_padding", 0.0))
    trail_act = pips_to_price(_f(template, "trail_activation", 0.0))
    trail_level = int(_f(template, "tp1_trigger_level", 0))
    # ApplyTemplateStepTrail's own minimum-move gate (mql5:2159-2171): a step
    # trail only actually moves the stop once the improvement over the LIVE
    # stop is >= trail_step pips, 0 meaning "move on any improvement" -- the
    # EA's own default when the key is missing. Every template created by
    # ea_templates.py's DEFAULTS carries trail_step=10.0, so omitting this
    # gate (as this walk did until 2026-09-03) trailed on every single-pip
    # wiggle a bar's high/low happened to produce -- tighter, more often,
    # than the EA the numbers are supposed to match.
    trail_step = pips_to_price(_f(template, "trail_step", 0.0))
    trail_armed = False

    res = TemplateResult(lot_size=lot, entry=entry, remaining_lots=lot)

    def _favourable(price: float) -> float:
        return (price - entry) * sign

    def _tighten(new_stop: float) -> None:
        """MoveSl: only ever accepts a stop closer to price than the current."""
        nonlocal stop
        if stop is None or (new_stop - stop) * sign > 0:
            stop = new_stop

    for idx, bar in enumerate(bars):
        high, low = float(bar["high"]), float(bar["low"])

        # The stop first: when one bar spans both the stop and a target, this
        # resolves the stop. Pessimistic on purpose -- see the module note.
        if stop is not None and ((low <= stop) if is_buy else (high >= stop)):
            res.outcome = "sl"
            res.close_price = stop
            res.close_bar = idx
            res.pnl_price += (stop - entry) * sign * res.remaining_lots
            res.remaining_lots = 0.0
            break

        if ladder_on and res.remaining_lots > 0:
            for i, (tp_price, pct) in enumerate(ladder):
                if triggered[i]:
                    continue
                reached = (high >= tp_price) if is_buy else (low <= tp_price)
                if not reached:
                    break                      # ordered: stop at the first gap
                is_last = (i == len(ladder) - 1)
                if is_last and (close_full_on_last or not partials_on):
                    res.pnl_price += (tp_price - entry) * sign * res.remaining_lots
                    res.closed_lots.append(round(res.remaining_lots, 2))
                    res.remaining_lots = 0.0
                    triggered[i] = True
                    res.outcome = "tp"
                    res.close_price = tp_price
                    res.close_bar = idx
                    break
                if not partials_on:
                    break                      # nothing closes until the last
                lots = min(round(lot * pct, 2), res.remaining_lots)
                triggered[i] = True            # latched, even at 0 lots
                if lots <= 0:
                    continue
                res.pnl_price += (tp_price - entry) * sign * lots
                res.closed_lots.append(lots)
                res.remaining_lots = round(res.remaining_lots - lots, 2)
                # A ladder whose levels sum to exactly 100% (the normal
                # case) zeroes remaining_lots right here, through the
                # ordinary partial-close path rather than the dedicated
                # "close everything" branch above -- which is the only
                # other place close_price/close_bar get set. Without this,
                # every such trade reports outcome="tp" with close_price
                # stuck at its 0.0 default.
                if res.remaining_lots <= 0:
                    res.close_price = tp_price
                    res.close_bar = idx

        if res.remaining_lots <= 0:
            if res.outcome == "timeout":
                res.outcome = "tp"
            break

        # Breakeven -- latched off `triggered[]`, not a live price test, so a
        # retrace cannot un-arm it. No separate "already done" flag: _tighten
        # never widens a stop, so re-applying this every bar is idempotent and
        # a flag would be state no test could distinguish.
        if 0 < be_level <= len(ladder) and triggered[be_level - 1]:
            _tighten(entry + sign * be_buffer if be_mode == "entry_buffer" else entry)

        # Trail arming: a pip distance in profit, OR a TP rung clearing.
        if not trail_armed:
            if trail_act > 0 and _favourable(high if is_buy else low) >= trail_act:
                trail_armed = True
            elif 0 < trail_level <= len(ladder) and triggered[trail_level - 1]:
                trail_armed = True

        if trail_armed:
            if trail_mode == "step" and trail_dist > 0:
                candidate = (high if is_buy else low) - sign * (trail_dist + trail_pad)
                move = (candidate - stop) * sign if stop is not None else None
                if stop is None or trail_step <= 0 or move >= trail_step:
                    _tighten(candidate)
            elif trail_mode == "candle" and idx > 0:
                prev = bars[idx - 1]
                _tighten(float(prev["low"]) if is_buy else float(prev["high"]))
            elif trail_mode == "tp":
                best = None
                for i, (tp_price, _pct) in enumerate(ladder):
                    if not triggered[i]:
                        break
                    best = tp_price
                if best is not None:
                    _tighten(best)

    if res.remaining_lots > 0 and res.outcome == "timeout" and bars:
        res.close_price = float(bars[-1]["close"])
        res.close_bar = len(bars) - 1
        res.pnl_price += (res.close_price - entry) * sign * res.remaining_lots

    res.pnl_usd = round(res.pnl_price * _USD_PER_PRICE_UNIT_PER_LOT, 2)
    return res


def simulate_ticks(template: dict, ticks: list, entry: float, is_buy: bool,
                    balance: float = 10_000.0,
                    stops_level_points: float = 0.0) -> TemplateResult:
    """Walk `ticks` under `template`, starting from a fill at `entry`.

    Resolved against actual bid/ask instead of a bar's high/low -- the
    same-bar "stop or target first" ambiguity that `simulate()` resolves
    pessimistically does not exist here, because a real tick can only be on
    one side of a level at a time.

    Every comparison uses the side ManageTemplate() itself reads off the
    live `MqlTick`: a BUY marks against `tick.bid` (what you could sell it for
    right now, matching TpCleared()'s `tick.bid >= val` and the `favMove`/
    `inProfit` reads), a SELL against `tick.ask`. Getting this backwards would
    make every fill look worse than the EA's for a BUY and better for a SELL,
    in a way that would not show up as an obviously wrong number -- it would
    just be quietly biased.

    Where this walk and `simulate()` differ, this one is the EA's. Read out of
    ManageTemplate() and MoveSl() on 2026-11-06, after a replay of real trades
    showed the difference:

      * Every stop move goes through MoveSl's clamp: no closer to price than
        max(stops level, guard_pips, safety_cap_pips). `stops_level_points`
        is SYMBOL_TRADE_STOPS_LEVEL, in points. History records no value for
        it, so the default of 0 leaves only the template's own two floors.
      * Breakeven is done when MoveSl SUCCEEDS, including with a clamped stop
        short of entry, and is not retried after that. It arms off the latch
        or, for a level that never trades, the live price.
      * The `tp` trail picks its level from the live price every tick, not
        from the latch, so it also moves on a level that never traded.
      * With no trail_activation the trail is always armed and
        tp1_trigger_level is not consulted. The activation is a live test.
      * The step gate measures the unclamped candidate.
      * A level with no lots to close does NOT latch: DoPartialClose returns
        before setting triggered[]. So it cannot arm tp1_trigger_level.
      * Partial closes and DoCloseAll trade at the tick. Only a broker-side TP
        fills at its level, and HandleOpenTrade sets one only for
        tpsl_mode=on with close_full_on_last.
      * be_trigger and tp1_trigger_level name a TP NUMBER. tp[] keeps its gaps
        on the EA side, so one naming an undefined level never arms.

    A stop still fills at its own price, gap or no gap: a real stop on a gap
    fills at the next price, which this does not model.

    `ticks` is `[{"time": ts, "bid": b, "ask": a}, ...]`, ascending by time.
    Raises UnsupportedTemplate for anything `simulate()` refuses, plus
    `trail_mode=candle` -- see `_SUPPORTED_TICK_TRAIL_MODES`.
    """
    _guard(template, tick_mode=True)

    sign = 1.0 if is_buy else -1.0
    sl_distance = pips_to_price(_f(template, "sl_pips", 0.0))
    lot = _lot_for(template, sl_distance, balance)

    stop: Optional[float] = (entry - sign * sl_distance) if sl_distance > 0 else None
    ladder = _ladder(template, entry, sign)
    triggered = [False] * len(ladder)
    # `ladder` drops undefined levels; the EA's triggers index tp[] with its
    # gaps intact, so a TP number has to be translated, not used as a position.
    tp_numbers = [n for n in range(1, _MAX_TP_LEVELS + 1)
                  if _f(template, f"tp{n}_pips", 0.0) > 0]

    def _position_of(tp_number: int) -> Optional[int]:
        return tp_numbers.index(tp_number) if tp_number in tp_numbers else None

    tpsl_mode = str(template.get("tpsl_mode") or "on").strip().lower()
    ladder_on = tpsl_mode != "off"
    partials_on = bool(_f(template, "partials", 1))
    close_full_on_last = bool(_f(template, "close_full_on_last", 0))
    broker_holds_last_tp = tpsl_mode == "on" and close_full_on_last

    be_position = _position_of(int(_f(template, "be_trigger", 0)))
    be_mode = str(template.get("be_mode") or "entry").strip().lower()
    be_buffer = _f(template, "be_buffer_pts", 0.0)
    be_done = False

    trail_mode = str(template.get("trail_mode") or "off").strip().lower()
    trail_dist = pips_to_price(_f(template, "trail_distance", 0.0))
    if trail_dist <= 0:
        trail_dist = _EA_DEFAULT_TRAIL_DISTANCE
    trail_pad = pips_to_price(_f(template, "trail_padding", 0.0))
    trail_act = pips_to_price(_f(template, "trail_activation", 0.0))
    trail_position = _position_of(int(_f(template, "tp1_trigger_level", 0)))
    trail_step = pips_to_price(_f(template, "trail_step", 0.0))

    min_dist = max(float(stops_level_points) * _POINT,
                   pips_to_price(_f(template, "guard_pips", 0.0)),
                   pips_to_price(_f(template, "safety_cap_pips", 0.0)))

    res = TemplateResult(lot_size=lot, entry=entry, remaining_lots=lot)

    def _cleared(position: int, mark: float) -> bool:
        return (mark - ladder[position][0]) * sign >= 0

    def _move_sl(wanted: float, mark: float) -> bool:
        """MoveSl. True when the stop actually moved, which is what the EA's
        breakeven latches on."""
        nonlocal stop
        if min_dist > 0:
            floor = mark - sign * min_dist
            if (wanted - floor) * sign > 0:
                wanted = floor
        # POSITION_SL reads 0.0 with no stop. Kept literal: a SELL's `better`
        # test can then never pass, so the EA never gives one a stop here.
        current = stop if stop is not None else 0.0
        if (wanted - current) * sign <= 0:
            return False
        # Only reachable with no floor at all. The broker refuses a stop on
        # the far side of price, so PositionModify fails and nothing moves.
        if (wanted - mark) * sign > 0:
            return False
        stop = wanted
        return True

    for idx, t in enumerate(ticks):
        mark = float(t["bid"]) if is_buy else float(t["ask"])

        if stop is not None and ((mark <= stop) if is_buy else (mark >= stop)):
            res.outcome = "sl"
            res.close_price = stop
            res.close_bar = idx
            res.pnl_price += (stop - entry) * sign * res.remaining_lots
            res.remaining_lots = 0.0
            break

        if ladder_on and res.remaining_lots > 0:
            for i, (tp_price, pct) in enumerate(ladder):
                if triggered[i]:
                    continue
                if not _cleared(i, mark):
                    break
                is_last = (i == len(ladder) - 1)
                if is_last and (close_full_on_last or not partials_on):
                    fill = tp_price if broker_holds_last_tp else mark
                    res.pnl_price += (fill - entry) * sign * res.remaining_lots
                    res.closed_lots.append(round(res.remaining_lots, 2))
                    res.remaining_lots = 0.0
                    triggered[i] = True
                    res.outcome = "tp"
                    res.close_price = fill
                    res.close_bar = idx
                    break
                if not partials_on:
                    break
                lots = min(round(lot * pct, 2), res.remaining_lots)
                if lots <= 0:
                    continue
                triggered[i] = True
                res.pnl_price += (mark - entry) * sign * lots
                res.closed_lots.append(lots)
                res.remaining_lots = round(res.remaining_lots - lots, 2)
                # A ladder whose levels sum to exactly 100% (the normal
                # case) zeroes remaining_lots right here, through the
                # ordinary partial-close path rather than the dedicated
                # "close everything" branch above -- which is the only
                # other place close_price/close_bar get set. Without this,
                # every such trade reports outcome="tp" with close_price
                # stuck at its 0.0 default.
                if res.remaining_lots <= 0:
                    res.close_price = mark
                    res.close_bar = idx

        if res.remaining_lots <= 0:
            if res.outcome == "timeout":
                res.outcome = "tp"
            break

        if not be_done and be_position is not None and (
                triggered[be_position] or _cleared(be_position, mark)):
            be_done = _move_sl(
                entry + sign * be_buffer if be_mode == "entry_buffer" else entry,
                mark)

        trail_armed = True
        if trail_act > 0:
            trail_armed = (mark - entry) * sign >= trail_act
            if not trail_armed and trail_position is not None:
                trail_armed = triggered[trail_position]

        if trail_armed:
            if trail_mode == "step":
                candidate = mark - sign * (trail_dist + trail_pad)
                if (stop is None or trail_step <= 0
                        or (candidate - stop) * sign >= trail_step):
                    _move_sl(candidate, mark)
            elif trail_mode == "tp":
                best = None
                for i, (tp_price, _pct) in enumerate(ladder):
                    if not _cleared(i, mark):
                        break
                    best = tp_price
                if best is not None:
                    _move_sl(best, mark)

    if res.remaining_lots > 0 and res.outcome == "timeout" and ticks:
        last = ticks[-1]
        res.close_price = float(last["bid"]) if is_buy else float(last["ask"])
        res.close_bar = len(ticks) - 1
        res.pnl_price += (res.close_price - entry) * sign * res.remaining_lots

    res.pnl_usd = round(res.pnl_price * _USD_PER_PRICE_UNIT_PER_LOT, 2)
    return res
