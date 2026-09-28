"""The Reversal Engine's LIMIT ORDER path runs a single-mode template's rules
(2026-09-28).

Found live on the VPS, ticket 2103677613: a Reversal Engine BUY filled at
4142.02 labelled "template:30 TP1 SL50 and Trail" but carrying the RE
signal's own 8-level ladder -- four of its TPs BELOW the fill -- a 103-pip
stop instead of the template's 50, and no template at all on the wire, so the
EA ran none of its partials, breakeven or trail. The broker TP was simply the
ladder's highest level.

`_try_re_limit_order` now does what the Limit Runner path has done since
limit-orders/020: the template's stop and ladder measured from the RESTING
price (where a limit fills), sized by `template_lot` from that stop, and the
template itself passed to `place_pending_order`. A strategy that is not a
template is sent exactly as before.

No broker is reached: the EA is a recording fake.
"""
import asyncio

import pytest

from backend.src.db import database as db
from backend.src.services.broker import ea_bridge as ea_mod
from backend.src.services.broker import ea_templates as et
from backend.src.services.reversal_engine import reversal_engine_repo as re_db
from backend.src.services.reversal_engine.reversal_engine_live_execute import _LiveExecuteMixin
from backend.src.services.signals import resolution
from backend.src.services.trading import close_trade
from backend.src.services.trading.fees_sizing import suggest_lot_size

TEMPLATE = "Single T"
# The live signal's shape: zone top 4142.00, and the RE's own ladder, which
# starts below it.
RE_LADDER = [4138.5, 4139.5, 4140.5, 4141.5, 4143.5, 4145.5, 4150.5, 4165.5]
BALANCE = 10_000.0


class _RecordingEA:
    def __init__(self):
        self.calls = []

    def is_ea_healthy(self):
        return True

    async def place_pending_order(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"type": "pending_order_placed", "ticket": 777}


@pytest.fixture
def run(monkeypatch, fresh_db):
    et.save_ea_template(TEMPLATE, {
        "mode": "single", "anchors": 1, "pendings": 0, "sl_pips": 50.0,
        "tp1_pips": 40.0, "tp2_pips": 50.0, "tp1_pct": 55.0, "tp2_pct": 10.0,
        "risk_pct": 1.0,
    })
    ea = _RecordingEA()
    stored = []
    monkeypatch.setattr(ea_mod, "get_instance", lambda: ea)
    monkeypatch.setattr(re_db, "claim_vantage_signal_activation", lambda _sid: 1)
    monkeypatch.setattr(re_db, "insert_vantage_pending_order", lambda row: stored.append(row))
    monkeypatch.setattr(re_db, "update_live_exec", lambda *a, **k: None)
    monkeypatch.setattr(re_db, "restore_vantage_signal_pending", lambda *_a: None)

    async def _balance(_bridge, _start):
        return BALANCE
    monkeypatch.setattr(close_trade, "get_trading_balance", _balance)

    def _go(strategy):
        async def _resolved(_bridge, _sid, tick=None):
            sig = {"direction": "BUY", "entry_low": 4139.0, "entry_high": 4142.0}
            sig.update({f"tp{n}": v for n, v in enumerate(RE_LADDER, start=1)})
            return {"strategy": strategy, "lot_size": 0.02,
                    "stop_loss_to_use": 4131.72, "sig": sig, "tick": None}
        monkeypatch.setattr(resolution, "resolve_open_trade_params", _resolved)
        mixin = _LiveExecuteMixin()
        mixin._bridge = None
        result = asyncio.run(mixin._try_re_limit_order(
            {"id": 1, "signal_ref": "RE-1"}, "vsig-1", None))
        assert result is True
        assert len(ea.calls) == 1, "exactly one resting order"
        args, kwargs = ea.calls[0]
        # place_pending_order(trade_id, direction, price, lot, stop, tps, ...)
        return {"price": args[2], "lot": args[3], "stop": args[4], "tps": args[5],
                "strategy": args[8], "template": kwargs.get("template"),
                "stored": stored[0]}
    return _go


def test_the_template_itself_is_sent(run):
    sent = run(et.override_for_template(TEMPLATE))
    assert sent["template"] is not None
    assert sent["template"]["name"] == TEMPLATE


def test_the_stop_is_the_templates_measured_from_the_resting_price(run):
    sent = run(et.override_for_template(TEMPLATE))
    assert sent["price"] == 4142.0
    assert sent["stop"] == pytest.approx(4137.0)      # 50 pips below 4142.00


def test_the_ladder_is_the_templates_and_every_tp_is_beyond_the_entry(run):
    sent = run(et.override_for_template(TEMPLATE))
    assert sent["tps"] == {1: pytest.approx(4146.0), 2: pytest.approx(4147.0)}
    assert all(tp > sent["price"] for tp in sent["tps"].values())


def test_the_lot_is_sized_from_the_stop_actually_sent(run):
    sent = run(et.override_for_template(TEMPLATE))
    assert sent["lot"] == pytest.approx(suggest_lot_size(4142.0, 4137.0, BALANCE, 1.0))


def test_the_stored_row_matches_what_was_sent(run):
    import json
    sent = run(et.override_for_template(TEMPLATE))
    row = sent["stored"]
    # (trade_id, sig_id, tg_id, source, direction, price, stop, tps_json, ...)
    assert row[6] == pytest.approx(sent["stop"])
    assert {int(k): v for k, v in json.loads(row[7]).items()} == sent["tps"]


def test_a_strategy_that_is_not_a_template_is_sent_as_before(run):
    sent = run("reversal_runner")
    assert sent["template"] is None
    assert sent["stop"] == 4131.72
    assert sent["lot"] == 0.02
    assert list(sent["tps"].values()) == RE_LADDER
