"""The tick walk, one tick at a time, with its state saved between ticks.

docs/todo/030. A Reversal virtual trade is managed one five-second poll at a
time and must survive a restart, so the walk's state is written to the signal
row after every tick and read back before the next. This pins that a walk
driven that way -- state through JSON on EVERY tick -- ends exactly where
`simulate_ticks` ends on the same ticks. Anything the saved state forgot
(the breakeven latch, a latched level, the stop) shows up here as a
different result.

`simulate_ticks` itself is pinned rule by rule in test_tick_simulator.py.
"""
from __future__ import annotations

import json
import random

import pytest

from backend.src.services.backtest import template_simulator as sim
from backend.src.services.backtest.template_tick_walk import TickWalk


def _tpl(**over) -> dict:
    base = {
        "name": "T", "mode": "single", "lot_anchor": 0.10, "risk_pct": 0.0,
        "sl_pips": 50.0, "tpsl_mode": "on", "partials": 1,
        "close_full_on_last": 0, "be_mode": "entry", "be_trigger": 0,
        "be_buffer_pts": 0.0, "trail_mode": "off", "trail_distance": 0.0,
        "trail_activation": 0.0, "trail_padding": 0.0, "tp1_trigger_level": 0,
        "tp1_pips": 0.0, "tp1_pct": 0.0,
    }
    base.update(over)
    return base


TEMPLATES = {
    "two partials, breakeven at TP1": _tpl(
        tp1_pips=30, tp1_pct=50, tp2_pips=100, tp2_pct=50, be_trigger=1),
    "close full on last, broker TP": _tpl(
        tp1_pips=20, tp1_pct=30, tp2_pips=60, tp2_pct=30, close_full_on_last=1),
    "step trail armed by distance": _tpl(
        tp1_pips=40, tp1_pct=40, trail_mode="step", trail_distance=25,
        trail_activation=20, trail_step=5),
    "tp trail with a guard": _tpl(
        tp1_pips=15, tp1_pct=25, tp2_pips=30, tp2_pct=25, tp3_pips=60,
        tp3_pct=25, trail_mode="tp", guard_pips=10),
    "breakeven with buffer, gap in ladder": _tpl(
        tp1_pips=20, tp1_pct=50, tp3_pips=80, tp3_pct=50, be_trigger=3,
        be_mode="entry_buffer", be_buffer_pts=0.5),
    "no partials": _tpl(tp1_pips=20, tp1_pct=50, tp2_pips=50, tp2_pct=50,
                        partials=0),
    "trail armed by TP level, safety cap": _tpl(
        tp1_pips=25, tp1_pct=60, trail_mode="step", trail_distance=30,
        trail_activation=500, tp1_trigger_level=1, safety_cap_pips=15),
}


def _path(seed: int, n: int = 400) -> list[dict]:
    """A random walk around 4000 with a moving spread."""
    rng = random.Random(seed)
    px, out = 4000.0, []
    for i in range(n):
        px += rng.gauss(0, 0.35)
        spread = round(rng.choice((0.1, 0.15, 0.2, 0.3)), 2)
        out.append({"time": i, "bid": round(px, 2), "ask": round(px + spread, 2)})
    return out


def _walk_with_restarts(template, ticks, entry, is_buy, lot):
    """Drive TickWalk with its state through JSON before every tick."""
    state = TickWalk(template, entry, is_buy, lot).to_state()
    pnl, closed, outcome, close_price, remaining = 0.0, [], "timeout", 0.0, lot
    sign = 1.0 if is_buy else -1.0
    for t in ticks:
        walk = TickWalk.from_state(template, json.loads(json.dumps(state)))
        for ev in walk.step(float(t["bid"]), float(t["ask"])):
            pnl += (ev.price - entry) * sign * ev.lots
            closed.append(ev.lots)
            close_price = ev.price
            if ev.kind == "sl":
                outcome = "sl"
                closed.pop()          # simulate_ticks lists only TP closes
            elif ev.kind == "close":
                outcome = "tp"
        remaining = walk.remaining_lots
        state = walk.to_state()
        if walk.done:
            if outcome == "timeout":
                outcome = "tp"
            break
    return round(pnl, 6), closed, outcome, remaining


@pytest.mark.parametrize("name", sorted(TEMPLATES))
@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("is_buy", (True, False))
def test_a_walk_saved_after_every_tick_ends_where_simulate_ticks_ends(name, seed, is_buy):
    template = TEMPLATES[name]
    ticks = _path(seed)
    entry = ticks[0]["ask"] if is_buy else ticks[0]["bid"]
    ref = sim.simulate_ticks(template, ticks, entry, is_buy)
    pnl, closed, outcome, remaining = _walk_with_restarts(
        template, ticks, entry, is_buy, ref.lot_size)
    if ref.outcome == "timeout":
        # simulate_ticks books the leftover at the last tick; a live walk
        # simply has not finished yet.
        assert outcome == "timeout"
        assert remaining == pytest.approx(ref.remaining_lots)
        return
    assert outcome == ref.outcome
    assert closed == ref.closed_lots
    assert pnl == pytest.approx(ref.pnl_price, abs=1e-9)


def test_the_paths_reach_every_kind_of_exit():
    """The equivalence above proves nothing about an exit no path reaches."""
    seen = set()
    for template in TEMPLATES.values():
        for seed in range(12):
            for is_buy in (True, False):
                ticks = _path(seed)
                entry = ticks[0]["ask"] if is_buy else ticks[0]["bid"]
                seen.add(sim.simulate_ticks(template, ticks, entry, is_buy).outcome)
    assert seen == {"sl", "tp", "timeout"}


def test_state_records_the_breakeven_latch():
    """Breakeven is done once MoveSl succeeds and is never retried. A saved
    state that dropped the latch would retry it after a restart."""
    template = TEMPLATES["two partials, breakeven at TP1"]
    walk = TickWalk(template, 4000.0, True, 0.10)
    walk.step(4003.2, 4003.4)                  # TP1 clears: partial, then BE
    state = walk.to_state()
    assert state["be_done"] is True
    assert state["stop"] == pytest.approx(4000.0)
    assert state["triggered"][0] is True
    again = TickWalk.from_state(template, json.loads(json.dumps(state)))
    assert again.stop == pytest.approx(4000.0)
    assert again.remaining_lots == pytest.approx(0.05)


def test_the_initial_stop_is_sl_pips_from_entry():
    walk = TickWalk(_tpl(sl_pips=50, tp1_pips=30, tp1_pct=100), 4000.0, False, 0.10)
    assert walk.stop == pytest.approx(4005.0)
    assert walk.risk == pytest.approx(5.0)
