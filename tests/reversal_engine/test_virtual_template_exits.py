"""A virtual Reversal signal stamped with a template runs on that template.

docs/todo/030. Every signal is stamped with the strategy a real order would
run under (schedule window, else the channel's pick, else the global one).
Until 2026-11-07 the virtual manager read the stamp only to spot
`conservative`; a `template:<name>` stamp fell through to the 50/30/20 REF
ladder on the signal's own stop and targets, so the ML learned outcomes of a
management no real order used.

Numbers: no main engine is attached, so `_net_pnl` returns gross, which is
points x 0.1 lot x $100 = $10 a point for the whole position.

ml_engine.record_outcome, close_bus_entry and push_trade_closed are patched
out, as in test_manage_strategy_dispatch.py: the first writes the real app's
model files.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.src.services.reversal_engine import virtual_template_exit as vte
from backend.src.services.reversal_engine.ml_engine._training_data import _realised_r
from backend.src.services.reversal_engine.reversal_engine_service import ReversalEngine


TEMPLATE = {
    "name": "Asian - Single", "mode": "single", "pendings": 0,
    "lot_anchor": 0.03, "risk_pct": 0.0, "sl_pips": 50.0,
    "tpsl_mode": "on", "partials": 1, "close_full_on_last": 0,
    "be_mode": "entry", "be_trigger": 1, "be_buffer_pts": 0.0,
    "trail_mode": "off", "trail_distance": 0.0, "trail_activation": 0.0,
    "trail_padding": 0.0, "tp1_trigger_level": 0,
    "tp1_pips": 30.0, "tp1_pct": 50.0, "tp2_pips": 100.0, "tp2_pct": 50.0,
}


@pytest.fixture
def engine(fresh_re_db):
    return ReversalEngine(bridge=None)


@pytest.fixture
def templates(monkeypatch):
    """name -> template; what `virtual_template_exit` looks templates up in."""
    store = {"Asian - Single": dict(TEMPLATE)}
    monkeypatch.setattr(vte, "_load_template", lambda name: store.get(name))
    return store


@pytest.fixture(autouse=True)
def _no_external_side_effects(monkeypatch):
    monkeypatch.setattr(
        "backend.src.services.reversal_engine.ml_engine.record_outcome",
        lambda signal_id, outcome: None)
    monkeypatch.setattr(
        "backend.src.db.database.close_bus_entry", lambda engine, signal_id: None)
    monkeypatch.setattr(
        "backend.src.services.cluster.sync.ledger.push_trade_closed", lambda trade: None)


def _tick(bid: float, ask: float):
    return SimpleNamespace(bid=bid, ask=ask, mid=(bid + ask) / 2, spread=round(ask - bid, 4))


def _buy(fresh_re_db, strategy="template:Asian - Single", **over) -> int:
    """A BUY filled at 4000. Its own stop (3996) and TP1 (4001.5) are where
    the ladder would act; the template's are 3995 and 4003."""
    data = {
        "signal_ref": "t-1", "direction": "BUY",
        "entry_low": 3999.0, "entry_high": 4001.0, "trigger_price": 4000.0,
        "stop_loss": 3996.0, "sl_dist": 4.0,
        "tp1": 4001.5, "tp4": 4020.0, "tp7": 4060.0,
        "status": "triggered", "outcome": "open", "strategy": strategy,
        "source_channel": "Reversal Engine",
        "remaining_frac": 1.0, "partial_pnl_dollars": 0.0,
    }
    data.update(over)
    return fresh_re_db.create_signal(data)


def _run(engine, fresh_re_db, sig_id, bid, ask):
    asyncio.run(engine._manage_triggered_signal(fresh_re_db.get_signal_by_id(sig_id), _tick(bid, ask)))
    return fresh_re_db.get_signal_by_id(sig_id)


def test_the_template_stop_not_the_signal_stop(engine, fresh_re_db, templates):
    sig_id = _buy(fresh_re_db)
    sig = _run(engine, fresh_re_db, sig_id, 3995.5, 3995.7)   # past 3996, short of 3995
    assert sig["status"] == "triggered"
    assert sig["remaining_frac"] == pytest.approx(1.0)
    assert sig["stop_loss"] == pytest.approx(3995.0)
    assert sig["virtual_risk_pts"] == pytest.approx(5.0)
    assert json.loads(sig["virtual_exit_json"])["model"] == "template"


def test_template_partial_breakeven_and_stop_out(engine, fresh_re_db, templates):
    sig_id = _buy(fresh_re_db)
    # 4001.6 clears the signal's TP1 (the ladder would bank 50% here) but
    # not the template's TP1 at 4003.
    sig = _run(engine, fresh_re_db, sig_id, 4001.6, 4001.8)
    assert sig["remaining_frac"] == pytest.approx(1.0)

    # TP1 4003 clears at bid 4003.2: half closes at the tick, +3.2 pts x
    # $10 x 0.5 = $16.00, and breakeven moves the stop to entry.
    sig = _run(engine, fresh_re_db, sig_id, 4003.2, 4003.4)
    assert sig["status"] == "triggered"
    assert sig["remaining_frac"] == pytest.approx(0.5)
    assert sig["partial_pnl_dollars"] == pytest.approx(16.0)
    assert sig["max_tp_hit"] == 1
    assert sig["stop_loss"] == pytest.approx(4000.0)

    # A restart: a new engine resumes from the row alone.
    fresh_engine = ReversalEngine(bridge=None)
    # Stop at 4000 hit at bid 3999.8 (inside the spread + 0.5 cap, so it
    # fills there): -0.2 pts x $10 x 0.5 = -$1.00. Total +$15.00, a win.
    sig = _run(fresh_engine, fresh_re_db, sig_id, 3999.8, 4000.0)
    assert sig["status"] == "closed"
    assert sig["outcome"] == "win"
    assert sig["net_pnl_dollars"] == pytest.approx(15.0)


def test_a_running_trade_keeps_the_template_it_started_with(engine, fresh_re_db, templates):
    sig_id = _buy(fresh_re_db)
    _run(engine, fresh_re_db, sig_id, 4003.2, 4003.4)
    templates["Asian - Single"]["tp2_pips"] = 20.0      # edited mid-trade
    sig = _run(engine, fresh_re_db, sig_id, 4002.5, 4002.7)
    assert sig["status"] == "triggered"                  # TP2 still 4010
    sig = _run(engine, fresh_re_db, sig_id, 4010.1, 4010.3)
    assert sig["status"] == "closed"
    assert sig["net_pnl_dollars"] == pytest.approx(16.0 + 50.5)


def test_a_sell_with_the_gd2_channel_still_takes_its_template(engine, fresh_re_db, templates):
    sig_id = fresh_re_db.create_signal({
        "signal_ref": "t-2", "direction": "SELL",
        "entry_low": 3999.0, "entry_high": 4001.0, "trigger_price": 4000.0,
        "stop_loss": 4002.0, "sl_dist": 2.0, "tp1": 3998.0, "tp2": 3996.0, "tp3": 3990.0,
        "status": "triggered", "outcome": "open", "strategy": "template:Asian - Single",
        "source_channel": "GOLD DIGGERS 2.0 ⚡️",
        "remaining_frac": 1.0, "partial_pnl_dollars": 0.0,
    })
    sig = _run(engine, fresh_re_db, sig_id, 4004.7, 4004.9)  # ask under 4005
    assert sig["status"] == "triggered"
    assert sig["remaining_frac"] == pytest.approx(1.0)
    sig = _run(engine, fresh_re_db, sig_id, 4004.9, 4005.1)
    assert sig["status"] == "closed"
    assert sig["outcome"] == "loss"


@pytest.mark.parametrize("change, why", [
    ({"harvest_enabled": 1}, "harvest"),
    ({"mode": "grid"}, "grid"),
    ({"sl_pips": 0.0}, "stop"),
    ({"trail_mode": "candle"}, "candle"),
])
def test_a_template_the_walk_cannot_model_keeps_the_ladder(engine, fresh_re_db, templates, change, why):
    templates["Asian - Single"].update(change)
    sig_id = _buy(fresh_re_db)
    sig = _run(engine, fresh_re_db, sig_id, 4001.6, 4001.8)   # the ladder's TP1
    assert sig["remaining_frac"] == pytest.approx(0.5)
    assert sig["max_tp_hit"] == 1
    assert sig["virtual_risk_pts"] is None
    saved = json.loads(sig["virtual_exit_json"])
    assert saved["model"] == "ladder"
    assert why in saved["reason"]


def test_a_template_that_no_longer_exists_keeps_the_ladder(engine, fresh_re_db, templates):
    sig_id = _buy(fresh_re_db, strategy="template:Deleted One")
    sig = _run(engine, fresh_re_db, sig_id, 4001.6, 4001.8)
    assert sig["remaining_frac"] == pytest.approx(0.5)
    saved = json.loads(sig["virtual_exit_json"])
    assert saved["model"] == "ladder"
    assert "Deleted One" in saved["reason"]


def test_a_non_template_strategy_is_untouched(engine, fresh_re_db, templates):
    sig_id = _buy(fresh_re_db, strategy="scale_out")
    sig = _run(engine, fresh_re_db, sig_id, 4001.6, 4001.8)
    assert sig["remaining_frac"] == pytest.approx(0.5)
    assert sig["virtual_exit_json"] is None


def test_the_label_divides_by_the_stop_actually_used():
    row = {"sl_dist": 4.0, "net_pnl_dollars": 25.0}
    assert _realised_r(row) == pytest.approx(25.0 / 40.0)
    assert _realised_r({**row, "virtual_risk_pts": 5.0}) == pytest.approx(0.5)
    assert _realised_r({**row, "virtual_risk_pts": None}) == pytest.approx(25.0 / 40.0)


def test_the_learning_panel_rows_carry_the_template_stop(fresh_re_db):
    sig_id = _buy(fresh_re_db, ml_prob=0.6, outcome="win", virtual_risk_pts=5.0)
    rows = [dict(r) for r in fresh_re_db.fetch_ml_outcome_rows()]
    assert [r["virtual_risk_pts"] for r in rows if r["id"] == sig_id] == [5.0]
