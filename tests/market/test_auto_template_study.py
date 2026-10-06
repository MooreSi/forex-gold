"""tools/auto_template_study (docs/todo/014): the pieces the map is derived
from. Nothing here reads a real database or reaches a broker."""
from backend.src.services.reversal_engine import entry_study as es
from tools import auto_template_study as ats

TEST = {"name": "Test", "mode": "single", "sl_pips": 50, "tp1_pips": 20, "tp1_pct": 40,
        "tp2_pips": 40, "tp2_pct": 40, "be_trigger": 1, "be_buffer_pts": 1.0,
        "trail_activation": 40, "trail_distance": 50, "trail_step": 10}


def test_a_template_becomes_points():
    p = ats.policy_for(TEST)
    assert (p.stop_pts, p.target_pts, p.target_frac, p.runner_target_pts) == (5.0, 2.0, 0.4, 4.0)
    assert p.be_trigger_pts == 2.0


def test_a_grid_template_is_skipped():
    assert ats.policy_for(dict(TEST, mode="grid")) is None


def test_a_source_label_folds_to_its_channel():
    assert ats.canon_source("Telegram Auto (Gold Diggers VIP)") == "Gold Diggers VIP"
    assert ats.canon_source("instant:GOLD DIGGERS INSTITUTIONAL") == "GOLD DIGGERS INSTITUTIONAL"


def _m1(prices, t0=0.0):
    return [es.Bar(t0 + 60 * i, p, p + 0.05, p - 0.05, p) for i, p in enumerate(prices)]


def test_a_straight_drop_is_a_full_loss_for_a_buy():
    bars = _m1([4100 - 0.5 * i for i in range(40)])
    pol = ats.policy_for(dict(TEST, be_trigger=0, trail_activation=0))
    res = es.replay(bars, es.Entry(0, 4100.0, 0.0), "BUY", pol)
    assert res.reason == "stop" and res.r_multiple < -1.0      # -1R plus cost


def test_h1_bars_are_built_from_whole_hours():
    h1 = ats.h1_from_m1(_m1([4100 + i * 0.1 for i in range(150)]))
    assert [b["ts"] for b in h1] == [0.0, 3600.0, 7200.0]
    assert ats.closed_h1_before(h1, 7199.0) == h1[:1]


def test_a_cell_needs_both_halves_positive():
    cells = {("X", "trend", "with", "A"): {"a": [0.2] * 20, "b": [0.1] * 20},
             ("X", "trend", "with", "B"): {"a": [0.9] * 20, "b": [-0.1] * 20},
             ("X", "range", "flat", "A"): {"a": [0.5] * 5, "b": [0.5] * 5}}
    best = ats.best_cells(cells)
    assert best[("X", "trend", "with")][0] == "A"
    assert best[("X", "range", "flat")] is None
