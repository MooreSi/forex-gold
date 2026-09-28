"""A single-mode EA Template placeholder stops holding a slot within minutes
(owner decision, 2026-09-28).

Found live on the VPS that afternoon: Max Open Trades (3) refused every signal
for hours with "3 open" while MT5 held ONE position. The other two were
"30 TP1 SL50 and Trail" placeholder rows from 09:36 and 09:39 -- ticket 0,
entry 0 -- waiting out the 24h `placeholder_no_fill_expiry_s`.

That 24h exists for GRID templates, whose legs can rest as limit orders and
fill hours later. A SINGLE-mode template opens one market anchor and stages
nothing, so once the broker has no position and no deal for it after a few
minutes, nothing can still be coming. It now expires on its own, shorter
clock (`placeholder_single_no_fill_expiry_s`, 300s), through the same
evidence-checked write-off the long expiry uses.

What must NOT change: a grid template, a template this node cannot find, a
live leg or an opening deal all keep the old behaviour. Nothing here reaches
a broker -- the fake bridge refuses any close.
"""
import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.services.broker import ea_templates
from backend.src.services.positions import core_template_placeholder_repair as repair
from backend.src.services.risk import expert_params as ep
# The existing fake, shared rather than copied: test_fixture_dedup holds the
# number of local _FakeBridge classes to a shrink-only baseline.
from tests.core.test_template_placeholder_repair import _FakeBridge

TRADE_ID = "3041d252-f618-4a"
SINGLE = "30 TP1 SL50 and Trail"
GRID = "Sig Gen Grid"


@pytest.fixture
def templates(fresh_db):
    ea_templates.save_ea_template(SINGLE, {"mode": "single", "anchors": 1, "pendings": 0})
    ea_templates.save_ea_template(GRID, {"mode": "grid", "anchors": 1, "pendings": 3})


def _insert(strategy_name, age_s):
    now = time.time() - age_s
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,entry_high,"
            "stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"sig-{TRADE_ID}", "Gold Diggers VIP", "BUY", 4148.45, 4150.45, 4145.0,
             0.03, "active", now),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,direction,"
            "entry_low,entry_high,entry_price,lot_size,remaining_lots,stop_loss,status,open_time,"
            "strategy,managed_by,tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (TRADE_ID, f"sig-{TRADE_ID}", 0, "BUY", 4148.45, 4150.45, 0.0, 0.03, 0.03,
             4145.0, "open", now, f"template:{strategy_name}", "ea", "Gold Diggers VIP"),
        )


def _status():
    with db.db() as conn:
        return conn.execute("SELECT status, exit_reason FROM vantage_simulated_trades "
                            "WHERE trade_id=?", (TRADE_ID,)).fetchone()


def _run(bridge=None):
    return asyncio.run(repair.repair_template_placeholders(bridge or _FakeBridge()))


def test_the_default_single_mode_expiry_is_5_minutes(fresh_db):
    assert repair.placeholder_single_no_fill_expiry_secs() == 300


def test_a_dead_single_mode_placeholder_is_expired_after_minutes(templates):
    _insert(SINGLE, age_s=repair.placeholder_single_no_fill_expiry_secs() + 60)
    assert _run() == 1
    assert tuple(_status()) == ("closed", "no_fill_expired")


def test_it_frees_the_trade_slot(templates):
    from backend.src.services.trading import signal_state_repo
    _insert(SINGLE, age_s=repair.placeholder_single_no_fill_expiry_secs() + 60)
    assert signal_state_repo.count_trade_slots_used() == 1
    _run()
    assert signal_state_repo.count_trade_slots_used() == 0


def test_one_just_under_the_short_expiry_is_left_alone(templates):
    # The EA's ack can take up to 60s and a fill event a little longer.
    _insert(SINGLE, age_s=repair.placeholder_single_no_fill_expiry_secs() - 60)
    assert _run() == 0
    assert _status()[0] == "open"


def test_a_grid_placeholder_keeps_the_long_expiry(templates):
    # Its legs may be resting as limit orders; that is what 24h is for.
    _insert(GRID, age_s=repair.placeholder_single_no_fill_expiry_secs() + 3600)
    assert _run() == 0
    assert _status()[0] == "open"


def test_a_template_this_node_cannot_find_keeps_the_long_expiry(templates):
    # Unknown mode is not single mode.
    _insert("Deleted Template", age_s=repair.placeholder_single_no_fill_expiry_secs() + 3600)
    assert _run() == 0
    assert _status()[0] == "open"


def test_a_live_leg_still_wins(templates):
    comment = repair._comment_prefix(TRADE_ID) + "a1"
    bridge = _FakeBridge(positions=[{"ticket": 2103198838, "open_price": 4151.03,
                                     "volume": 0.03, "comment": comment}])
    _insert(SINGLE, age_s=repair.placeholder_single_no_fill_expiry_secs() + 60)
    assert _run(bridge) == 1
    status, reason = _status()
    assert status == "open" and reason != "no_fill_expired"


def test_the_short_expiry_follows_the_tunable(templates):
    ep.set_params({"placeholder_single_no_fill_expiry_s": 120})
    try:
        _insert(SINGLE, age_s=200)
        assert _run() == 1
    finally:
        ep.set_params({"placeholder_single_no_fill_expiry_s": 300})
