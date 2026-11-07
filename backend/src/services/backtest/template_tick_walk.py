"""The EA template's tick rules, one tick at a time, with saveable state.

docs/todo/030. `template_simulator.simulate_ticks` walks a whole tick series
in one call. A Reversal virtual trade sees one tick per five-second poll and
must survive a restart between any two of them, so the same rules are held
here as a stepper whose state is a plain dict. `simulate_ticks` drives this
class, so there is one copy of the rules: the Backtest tab and the virtual
trades cannot drift apart.

The rules and their reasons are in `simulate_ticks`' docstring (read out of
ManageTemplate() and MoveSl() on 2026-11-06). This class does not guard the
template: callers ask `template_simulator.unsupported_reason` first.

Pure: prices in, events out. No database, no broker, no clock.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = ["TickWalk", "Event"]

# InpDefaultTrailStopPts (default 5.0): the EA's fallback when trail_distance
# is 0, a raw price delta rather than pips. An EA input, so a chart can differ.
_EA_DEFAULT_TRAIL_DISTANCE = 5.0
_MAX_TP_LEVELS = 8
_POINT = 0.01


@dataclass(frozen=True)
class Event:
    """Something closed. `kind` is "partial" (a TP level closed `lots` at the
    tick), "close" (the last level closed what remained) or "sl" (the stop,
    at its own price). `tp_number` is the template's TP number, gaps kept."""
    kind: str
    price: float
    lots: float
    tp_number: Optional[int] = None


def _f(template: dict, key: str, default: float = 0.0) -> float:
    try:
        v = template.get(key)
        return float(default if v is None or v == "" else v)
    except (TypeError, ValueError):
        return default


def _pips(p: float) -> float:
    return float(p) * 10.0 * _POINT


class TickWalk:
    """One template trade from a fill at `entry`, `lot` lots."""

    def __init__(self, template: dict, entry: float, is_buy: bool, lot: float,
                 stops_level_points: float = 0.0):
        from backend.src.services.backtest.template_simulator import _ladder

        self.template = template
        self.entry = float(entry)
        self.is_buy = bool(is_buy)
        self.lot = float(lot)
        self.stops_level_points = float(stops_level_points)
        self._sign = 1.0 if self.is_buy else -1.0
        self.risk = _pips(_f(template, "sl_pips", 0.0))
        self.stop: Optional[float] = (self.entry - self._sign * self.risk) if self.risk > 0 else None
        self._ladder = _ladder(template, self.entry, self._sign)
        self.triggered = [False] * len(self._ladder)
        self.remaining_lots = self.lot
        self.be_done = False
        self.done = False
        self._configure()

    def _configure(self) -> None:
        t = self.template
        # `_ladder` drops undefined levels; the EA's triggers index tp[] with
        # its gaps intact, so a TP number is translated, not used as a position.
        self._tp_numbers = [n for n in range(1, _MAX_TP_LEVELS + 1)
                            if _f(t, f"tp{n}_pips", 0.0) > 0]
        tpsl_mode = str(t.get("tpsl_mode") or "on").strip().lower()
        self._ladder_on = tpsl_mode != "off"
        self._partials_on = bool(_f(t, "partials", 1))
        self._close_full_on_last = bool(_f(t, "close_full_on_last", 0))
        self._broker_holds_last_tp = tpsl_mode == "on" and self._close_full_on_last
        self._be_position = self._position_of(int(_f(t, "be_trigger", 0)))
        self._be_mode = str(t.get("be_mode") or "entry").strip().lower()
        self._be_buffer = _f(t, "be_buffer_pts", 0.0)
        self._trail_mode = str(t.get("trail_mode") or "off").strip().lower()
        self._trail_dist = _pips(_f(t, "trail_distance", 0.0))
        if self._trail_dist <= 0:
            self._trail_dist = _EA_DEFAULT_TRAIL_DISTANCE
        self._trail_pad = _pips(_f(t, "trail_padding", 0.0))
        self._trail_act = _pips(_f(t, "trail_activation", 0.0))
        self._trail_position = self._position_of(int(_f(t, "tp1_trigger_level", 0)))
        self._trail_step = _pips(_f(t, "trail_step", 0.0))
        self._min_dist = max(self.stops_level_points * _POINT,
                             _pips(_f(t, "guard_pips", 0.0)),
                             _pips(_f(t, "safety_cap_pips", 0.0)))

    def _position_of(self, tp_number: int) -> Optional[int]:
        return self._tp_numbers.index(tp_number) if tp_number in self._tp_numbers else None

    # ── saved state ──────────────────────────────────────────────────────

    def to_state(self) -> dict:
        return {"entry": self.entry, "is_buy": self.is_buy, "lot": self.lot,
                "stops_level_points": self.stops_level_points,
                "stop": self.stop, "triggered": list(self.triggered),
                "remaining_lots": self.remaining_lots, "be_done": self.be_done,
                "done": self.done}

    @classmethod
    def from_state(cls, template: dict, state: dict) -> "TickWalk":
        walk = cls(template, state["entry"], state["is_buy"], state["lot"],
                   state.get("stops_level_points", 0.0))
        walk.stop = state["stop"]
        walk.triggered = [bool(x) for x in state["triggered"]]
        walk.remaining_lots = float(state["remaining_lots"])
        walk.be_done = bool(state["be_done"])
        walk.done = bool(state.get("done", False))
        return walk

    # ── the rules ────────────────────────────────────────────────────────

    def _cleared(self, position: int, mark: float) -> bool:
        return (mark - self._ladder[position][0]) * self._sign >= 0

    def _move_sl(self, wanted: float, mark: float) -> bool:
        """MoveSl. True when the stop actually moved, which is what the EA's
        breakeven latches on."""
        sign = self._sign
        if self._min_dist > 0:
            floor = mark - sign * self._min_dist
            if (wanted - floor) * sign > 0:
                wanted = floor
        # POSITION_SL reads 0.0 with no stop. Kept literal: a SELL's `better`
        # test can then never pass, so the EA never gives one a stop here.
        current = self.stop if self.stop is not None else 0.0
        if (wanted - current) * sign <= 0:
            return False
        # Only reachable with no floor at all. The broker refuses a stop on
        # the far side of price, so PositionModify fails and nothing moves.
        if (wanted - mark) * sign > 0:
            return False
        self.stop = wanted
        return True

    def step(self, bid: float, ask: float) -> list[Event]:
        """Apply one tick. Returns what closed on it, in order."""
        if self.done:
            return []
        sign = self._sign
        mark = float(bid) if self.is_buy else float(ask)
        events: list[Event] = []

        if self.stop is not None and ((mark <= self.stop) if self.is_buy else (mark >= self.stop)):
            events.append(Event("sl", self.stop, self.remaining_lots))
            self.remaining_lots = 0.0
            self.done = True
            return events

        if self._ladder_on and self.remaining_lots > 0:
            for i, (_tp_price, pct) in enumerate(self._ladder):
                if self.triggered[i]:
                    continue
                if not self._cleared(i, mark):
                    break
                is_last = (i == len(self._ladder) - 1)
                if is_last and (self._close_full_on_last or not self._partials_on):
                    fill = self._ladder[i][0] if self._broker_holds_last_tp else mark
                    events.append(Event("close", fill, round(self.remaining_lots, 2),
                                        self._tp_numbers[i]))
                    self.remaining_lots = 0.0
                    self.triggered[i] = True
                    break
                if not self._partials_on:
                    break
                lots = min(round(self.lot * pct, 2), self.remaining_lots)
                if lots <= 0:
                    continue
                self.triggered[i] = True
                self.remaining_lots = round(self.remaining_lots - lots, 2)
                events.append(Event("partial", mark, lots, self._tp_numbers[i]))

        if self.remaining_lots <= 0:
            self.done = True
            return events

        if not self.be_done and self._be_position is not None and (
                self.triggered[self._be_position] or self._cleared(self._be_position, mark)):
            self.be_done = self._move_sl(
                self.entry + sign * self._be_buffer if self._be_mode == "entry_buffer"
                else self.entry, mark)

        trail_armed = True
        if self._trail_act > 0:
            trail_armed = (mark - self.entry) * sign >= self._trail_act
            if not trail_armed and self._trail_position is not None:
                trail_armed = self.triggered[self._trail_position]

        if trail_armed:
            if self._trail_mode == "step":
                candidate = mark - sign * (self._trail_dist + self._trail_pad)
                if (self.stop is None or self._trail_step <= 0
                        or (candidate - self.stop) * sign >= self._trail_step):
                    self._move_sl(candidate, mark)
            elif self._trail_mode == "tp":
                best = None
                for i, (tp_price, _pct) in enumerate(self._ladder):
                    if not self._cleared(i, mark):
                        break
                    best = tp_price
                if best is not None:
                    self._move_sl(best, mark)
        return events
