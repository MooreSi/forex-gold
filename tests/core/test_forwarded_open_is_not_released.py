"""A signal the Mac forwarded to the VPS is not released back to pending
(bugs/071 #3).

2026-09-29, signal d32e37ed (RE-1AE5AB): the Mac, in Remote mode with
centralized signal generation, claimed it (`activating`), forwarded the open
to the VPS, and got a ticket back. `open_trade` returns `executed_remotely`
before any local `insert_trade`, and `insert_trade` is the only thing that
moves a signal to `active`, so the claim stayed `activating`. Fifteen
minutes later `release_stranded_activations` put it back to `pending` ("the
process died mid-open") and the PendingWatcher forwarded it again, three
times. Only the VPS's max-open-trades cap stopped a second real trade.

The forwarded success now moves the Mac's signal to `active`, which also
frees the slot the claim was holding there.

Nothing reaches a broker or a VPS: the sync client is a recording fake.
"""
import asyncio
import time
from unittest.mock import patch

from backend.src.db import database as db
from backend.src.services.trading import open_trade as ot
from backend.src.services.trading import signal_state_repo as ssr

from tests.core.test_open_trade_surface import (  # noqa: F401
    _FakeBridge, _FakeSyncClient, _insert_signal, _open_kwargs, fresh_db,
)


def _status(sig="sig-1"):
    with db.db() as conn:
        return conn.execute("SELECT status FROM vantage_signals WHERE signal_id=?",
                            (sig,)).fetchone()[0]


def _forward(client):
    db.set_app_config("sync_remote_host", "10.0.0.5")
    db.update_risk_settings({"centralized_signal_gen_enabled": 1, "max_open_trades": 5})
    with patch("backend.src.services.cluster.sync.client.get_instance", return_value=client):
        return asyncio.run(ot.open_trade(_FakeBridge(), **_open_kwargs()))


def test_a_forwarded_open_marks_the_signal_active(fresh_db):
    _insert_signal()
    assert ssr.claim_signal_activation("sig-1") == 1
    result = _forward(_FakeSyncClient())
    assert result["executed_remotely"] is True
    assert _status() == "active"


def test_the_stranded_sweep_does_not_put_it_back(fresh_db):
    _insert_signal()
    ssr.claim_signal_activation("sig-1")
    _forward(_FakeSyncClient())
    with db.db() as conn:   # sixteen minutes later
        conn.execute("UPDATE vantage_signals SET activated_at=? WHERE signal_id='sig-1'",
                     (time.time() - 16 * 60,))
    ssr.release_stranded_activations()
    assert _status() == "active"


def test_a_rejected_forward_leaves_it_for_the_caller(fresh_db):
    _insert_signal()
    ssr.claim_signal_activation("sig-1")
    try:
        _forward(_FakeSyncClient(ack={"type": "signal_order_ack", "error": "no slot"}))
    except RuntimeError:
        pass
    assert _status() == "activating"


def test_a_cancelled_signal_is_not_resurrected(fresh_db):
    _insert_signal()
    with db.db() as conn:
        conn.execute("UPDATE vantage_signals SET status='cancelled' WHERE signal_id='sig-1'")
    _forward(_FakeSyncClient())
    assert _status() == "cancelled"
