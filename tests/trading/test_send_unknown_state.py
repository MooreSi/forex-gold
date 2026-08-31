"""A lost response is not a rejection (stage3/020).

Two paths used to read "no answer" as "did not fill", and both can double-fire
a live order:

  * `mt5_bridge._place_order` walks three filling modes. On `order_send`
    returning None it CONTINUED to the next mode and sent again. None means
    the response was lost, not that nothing filled -- so if the first send
    did fill, the retry opens a second position.
  * `open_from_signal` restores the signal to `pending` on ANY exception, so a
    transport timeout put a possibly-filled signal back in the queue to be
    opened again.

The rule these tests pin: a no-response send parks the signal in `unknown`.
Not pending, not failed, not retryable. Only reconciliation (stage3/030) may
resolve it from broker truth. An explicit broker REJECTION is untouched and
still retryable, because a retcode saying no is real information.

No order is placed anywhere here. MetaTrader5 is a stub.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types

import pytest

from backend.src.services.trading import trade_repo


# ── the bridge half ──────────────────────────────────────────────────────────

def _load_bridge():
    """mt5_bridge runs under a different interpreter in production. It imports
    cleanly against a stubbed MetaTrader5, which is enough to exercise the
    send loop."""
    sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))
    spec = importlib.util.spec_from_file_location("_mt5_bridge_under_test",
                                                  "mt5_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Info:
    volume_step = 0.01
    volume_min = 0.01
    volume_max = 100.0
    trade_mode = 4
    digits = 2
    point = 0.01


class _Term:
    trade_allowed = True


class _Tick:
    bid = 3999.0
    ask = 4000.0


class _FakeMt5:
    """Counts sends. Returns whatever the test tells it to."""

    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = 10009
    ORDER_TIME_GTC = 0
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_RETURN = 2
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0

    def __init__(self, results):
        self._results = list(results)
        self.sends = 0

    def symbol_info_tick(self, *_a):
        return _Tick()

    def symbol_info(self, *_a):
        return _Info()

    def terminal_info(self):
        return _Term()

    def last_error(self):
        return (1, "stubbed")

    def order_send(self, request):
        self.sends += 1
        return self._results.pop(0) if self._results else None


class _Ok:
    retcode = 10009
    order = 5150
    price = 4000.0
    volume = 0.1
    comment = "done"


class _FillReject:
    """A real rejection saying the filling mode is wrong (retcode 10030)."""
    retcode = 10030
    order = 0
    price = 0.0
    volume = 0.0
    comment = "Unsupported filling mode"


@pytest.fixture
def bridge(monkeypatch):
    mod = _load_bridge()
    monkeypatch.setattr(mod, "_ensure_connected", lambda: True, raising=False)
    return mod


class TestTheBridgeDoesNotRetryOnALostResponse:
    def test_a_None_result_SENDS_EXACTLY_ONCE(self, bridge, monkeypatch):
        """The C3 double-fill. None is "the response was lost", and the order
        may well have filled; walking on to the next filling mode sends a
        second one."""
        fake = _FakeMt5([None, _Ok()])
        monkeypatch.setattr(bridge, "mt5", fake)

        bridge._place_order("BUY", 0.1, 3990.0, None, "py:abc")

        assert fake.sends == 1, (
            f"a lost response was retried ({fake.sends} sends) — this is how a "
            "filled order becomes two")

    def test_a_None_result_is_reported_as_UNKNOWN_not_as_a_failure(self, bridge,
                                                                   monkeypatch):
        """The caller has to be able to tell "no answer" from "rejected", or
        it cannot decide whether re-sending is safe."""
        fake = _FakeMt5([None])
        monkeypatch.setattr(bridge, "mt5", fake)

        res = bridge._place_order("BUY", 0.1, 3990.0, None, "py:abc")

        assert res.get("unknown") is True
        assert "error" in res

    def test_a_FILLING_MODE_REJECTION_is_still_retried(self, bridge, monkeypatch):
        """The negative control. 10030 is the broker explicitly saying "wrong
        filling mode, nothing filled" -- real information, and walking to the
        next mode is the right response. Only None must stop."""
        fake = _FakeMt5([_FillReject(), _Ok()])
        monkeypatch.setattr(bridge, "mt5", fake)

        res = bridge._place_order("BUY", 0.1, 3990.0, None, "py:abc")

        assert fake.sends == 2, "a legitimate filling-mode retry was removed"
        assert res.get("success") is True

    def test_a_successful_send_sends_once(self, bridge, monkeypatch):
        fake = _FakeMt5([_Ok()])
        monkeypatch.setattr(bridge, "mt5", fake)

        res = bridge._place_order("BUY", 0.1, 3990.0, None, "py:abc")

        assert fake.sends == 1
        assert res["ticket"] == 5150


# ── the signal-state half ────────────────────────────────────────────────────

def _insert_signal(signal_id="sig-u1", status="activating"):
    from backend.src.db.database import db
    import time as _t
    with db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,"
            "entry_low,entry_high,stop_loss,lot_size,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, "Test", "BUY", 4000.0, 4002.0, 3990.0, 0.1, status, _t.time()),
        )


def _status(signal_id="sig-u1"):
    from backend.src.db.database import db
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id=?", (signal_id,)
        ).fetchone()
        return row[0] if row else None


class TestParkingASignalAsUnknown:
    def test_it_moves_an_activating_signal_to_unknown(self, fresh_db):
        _insert_signal()

        trade_repo.park_signal_unknown("sig-u1", "send timed out")

        assert _status() == "unknown"

    def test_it_records_WHY_for_the_reconciler(self, fresh_db):
        """stage3/030 has to be able to tell what happened without the log."""
        from backend.src.db.database import db
        _insert_signal()

        trade_repo.park_signal_unknown("sig-u1", "send timed out after 15s")

        with db() as conn:
            notes = conn.execute(
                "SELECT notes FROM vantage_signals WHERE signal_id=?", ("sig-u1",)
            ).fetchone()[0]
        assert "timed out" in (notes or "")

    def test_it_does_NOT_touch_a_signal_that_is_not_activating(self, fresh_db):
        """Only the in-flight claim may be parked. Reaching a closed or
        cancelled signal would resurrect it."""
        _insert_signal(status="closed")

        trade_repo.park_signal_unknown("sig-u1", "send timed out")

        assert _status() == "closed"

    def test_an_unknown_signal_is_NOT_pending(self, fresh_db):
        """The whole point: pending is retryable, unknown is not."""
        _insert_signal()

        trade_repo.park_signal_unknown("sig-u1", "send timed out")

        assert _status() != "pending"


class TestTheSchedulerSkipsUnknown:
    def test_the_pending_query_does_not_return_an_unknown_signal(self, fresh_db):
        """If this ever stopped being true, a possibly-filled signal would be
        opened a second time -- exactly what parking it prevents."""
        from backend.src.services.signals import repo as signals_repo

        _insert_signal(signal_id="sig-pending", status="pending")
        _insert_signal(signal_id="sig-unknown", status="unknown")

        ids = [r["signal_id"]
               for r in signals_repo.get_pending_signals_awaiting_zone_fill()]

        assert "sig-pending" in ids
        assert "sig-unknown" not in ids


# ── routing: which failures park, which stay retryable ───────────────────────

class _Bridge:
    def __init__(self, place_result=None, raises=None):
        self._place_result = place_result or {"ticket": 999, "fill_price": 4000.0}
        self._raises = raises
        self.place_calls = 0

    def is_configured(self):
        return True

    async def get_positions(self):
        return []

    async def get_deal_history(self, days=7):
        return []

    async def get_fresh_tick(self):
        return _Tick()

    async def place_order(self, direction, lots, sl, tp, comment=""):
        self.place_calls += 1
        if self._raises:
            raise self._raises
        return self._place_result


class TestABridgeUnknownIsNotARejection:
    def test_an_unknown_place_result_raises_SendOutcomeUnknown(self, fresh_db):
        """A rejection and a lost response must not arrive at the caller as
        the same kind of failure: one is retryable, the other is not."""
        from backend.src.services.trading.send_dedup import SendOutcomeUnknown
        from backend.src.services.trading import open_trade as ot

        with pytest.raises(SendOutcomeUnknown):
            ot._raise_if_send_unknown({"error": "order_send returned None",
                                       "unknown": True})

    def test_a_plain_rejection_does_not(self, fresh_db):
        from backend.src.services.trading import open_trade as ot

        ot._raise_if_send_unknown({"error": "Invalid stops"})   # must not raise

    def test_a_success_does_not(self, fresh_db):
        from backend.src.services.trading import open_trade as ot

        ot._raise_if_send_unknown({"ticket": 999})


class TestOpenFromSignalRouting:
    """Which failures put the signal back in the queue, and which park it."""

    @staticmethod
    def _park_and_restore_spies(monkeypatch):
        seen = {"parked": [], "restored": []}
        from backend.src.services.trading import open_from_signal as ofs
        monkeypatch.setattr(ofs.trade_repo, "park_signal_unknown",
                            lambda sid, reason: seen["parked"].append((sid, reason)))
        monkeypatch.setattr(ofs.trade_repo, "restore_signal_after_failed_open",
                            lambda sid: seen["restored"].append(sid))
        return seen

    def test_an_UNKNOWN_send_parks_the_signal(self, fresh_db, monkeypatch):
        from backend.src.services.trading import open_from_signal as ofs
        from backend.src.services.trading.send_dedup import SendOutcomeUnknown

        seen = self._park_and_restore_spies(monkeypatch)

        ofs._route_failed_open("sig-1", SendOutcomeUnknown("no answer from broker"))

        assert seen["parked"] and seen["parked"][0][0] == "sig-1"
        assert seen["restored"] == [], "an unknown send was made retryable again"

    def test_a_TIMEOUT_parks_the_signal(self, fresh_db, monkeypatch):
        """A 15s HTTP timeout on order_send was treated as a rejection and the
        signal restored to pending. If the order filled, that is a live
        position nobody is tracking plus a second order on the retry."""
        seen = self._park_and_restore_spies(monkeypatch)
        from backend.src.services.trading import open_from_signal as ofs

        ofs._route_failed_open("sig-1", asyncio.TimeoutError())

        assert seen["parked"], "a send timeout was treated as a rejection"
        assert seen["restored"] == []

    @pytest.mark.parametrize("err", [
        ConnectionError("connection reset"),
        OSError("network is unreachable"),
    ])
    def test_a_TRANSPORT_error_parks_the_signal(self, fresh_db, monkeypatch, err):
        seen = self._park_and_restore_spies(monkeypatch)
        from backend.src.services.trading import open_from_signal as ofs

        ofs._route_failed_open("sig-1", err)

        assert seen["parked"]
        assert seen["restored"] == []

    def test_a_REJECTION_stays_retryable(self, fresh_db, monkeypatch):
        """The negative control. A broker retcode saying no is real
        information -- nothing filled, and the signal should go back in the
        queue exactly as it does today. If this parked, a rejected trade would
        need manual intervention every time."""
        seen = self._park_and_restore_spies(monkeypatch)
        from backend.src.services.trading import open_from_signal as ofs

        ofs._route_failed_open("sig-1", RuntimeError("MT5 order rejected: Invalid stops"))

        assert seen["restored"] == ["sig-1"]
        assert seen["parked"] == [], "a plain rejection was parked as unknown"

    def test_a_VALUE_ERROR_stays_retryable(self, fresh_db, monkeypatch):
        """Guard rejections (max open trades, circuit breaker) never reached
        the broker at all, so nothing can have filled."""
        seen = self._park_and_restore_spies(monkeypatch)
        from backend.src.services.trading import open_from_signal as ofs

        ofs._route_failed_open("sig-1", ValueError("Max open trades reached (5)"))

        assert seen["restored"] == ["sig-1"]
        assert seen["parked"] == []


class TestTheRestorePathIsGuardedToo:
    """Not new behaviour -- `restore_signal_after_failed_open` has always been
    guarded on status='activating'. It had no test, which mutation exposed
    while I was checking my own: dropping its guard passed everything."""

    def test_it_restores_an_activating_signal(self, fresh_db):
        _insert_signal(status="activating")

        trade_repo.restore_signal_after_failed_open("sig-u1")

        assert _status() == "pending"

    def test_it_does_NOT_resurrect_a_closed_signal(self, fresh_db):
        """A late error arriving after the signal has already been closed or
        cancelled must not put it back in the queue to be traded again."""
        _insert_signal(status="closed")

        trade_repo.restore_signal_after_failed_open("sig-u1")

        assert _status() == "closed"

    def test_it_does_not_resurrect_an_unknown_signal(self, fresh_db):
        """The interaction that matters now: once parked, only reconciliation
        may move it. A stray restore would undo the whole point of parking."""
        _insert_signal(status="unknown")

        trade_repo.restore_signal_after_failed_open("sig-u1")

        assert _status() == "unknown"


class TestTheOTHERResetPathIsGuardedToo:
    """Found on 2026-08-31 by driving the 020 killer demo end-to-end.

    There are TWO functions that put a signal back in the queue, and 020 only
    guarded one of them. `restore_signal_after_failed_open` is guarded (above);
    `reset_signal_to_pending` was not, and it is the one the Telegram
    auto-execute path calls -- on EVERY exception, including the
    `SendOutcomeUnknown` that `_route_failed_open` had just parked the signal
    for one frame below.

    So on the main signal path the park was written and then immediately
    overwritten, leaving the signal 'pending' -- the exact state PendingWatcher
    re-activates every 20 seconds. Every unit test passed, because none of them
    ran the caller.
    """

    def test_it_resets_an_activating_signal(self, fresh_db):
        _insert_signal(status="activating")

        trade_repo.reset_signal_to_pending("sig-u1")

        assert _status() == "pending"

    def test_it_resets_an_active_signal(self, fresh_db):
        """The stood-down path resets a signal that is merely 'active'."""
        _insert_signal(status="active")

        trade_repo.reset_signal_to_pending("sig-u1")

        assert _status() == "pending"

    def test_it_does_NOT_resurrect_an_unknown_signal(self, fresh_db):
        """The bug. A signal parked because nobody knows whether it filled must
        not be handed back to the scheduler by the generic error handler that
        runs on the way out."""
        _insert_signal(status="unknown")

        trade_repo.reset_signal_to_pending("sig-u1")

        assert _status() == "unknown"


class TestParkingWorksFromBothInFlightStates:
    """Also found by the 020 killer demo, 2026-08-31.

    `park_signal_unknown` was guarded on status='activating' only. That is the
    status `open_trade_from_signal` leaves a signal in while it opens -- but
    the fresh-Telegram-signal path does NOT go through `open_trade_from_signal`
    at all (it calls `core_open_trade.open_trade` directly), and on that path
    the signal is 'active' when the send fails.

    So on the primary signal path the park was a silent no-op: the UPDATE
    matched no row, and the signal stayed re-claimable, since the activation
    claim accepts `status IN ('pending','active')`.

    Both in-flight states must park. Nothing else may.
    """

    def test_it_parks_an_activating_signal(self, fresh_db):
        _insert_signal(status="activating")

        trade_repo.park_signal_unknown("sig-u1", "no answer")

        assert _status() == "unknown"

    def test_it_parks_an_ACTIVE_signal(self, fresh_db):
        """The gap. This is the state the Telegram path is actually in."""
        _insert_signal(status="active")

        trade_repo.park_signal_unknown("sig-u1", "no answer")

        assert _status() == "unknown"

    @pytest.mark.parametrize("status", ["closed", "cancelled", "pending", "failed"])
    def test_it_does_not_park_anything_else(self, fresh_db, status):
        """The guard still has a job: a late error must not reach back and
        park a signal that is already finished, or one that never started."""
        _insert_signal(status=status)

        trade_repo.park_signal_unknown("sig-u1", "no answer")

        assert _status() == status


class TestHttpxErrorsAreClassifiedToo:
    """Found 2026-08-31. `httpx.ReadTimeout` is not a builtin `TimeoutError`,
    and httpx's exception tree inherits from none of the types
    `_NO_ANSWER_ERRORS` lists -- so one escaping the bridge client was read as
    an ordinary rejection and the signal handed back to the scheduler.

    The client normally returns a dict rather than raising, so this is a
    backstop. It exists because the classification lived in exactly one place
    and that place did not know about the transport library actually in use.
    """

    def _exc(self, name):
        import httpx
        return getattr(httpx, name)("boom")

    @pytest.mark.parametrize("name", [
        "ReadTimeout", "ReadError", "WriteTimeout", "WriteError",
        "RemoteProtocolError",
    ])
    def test_a_lost_answer_is_unknown(self, name):
        from backend.src.services.trading.send_dedup import send_outcome_is_unknown

        assert send_outcome_is_unknown(self._exc(name)) is True

    @pytest.mark.parametrize("name", ["ConnectError", "ConnectTimeout",
                                      "PoolTimeout"])
    def test_a_failure_to_CONNECT_stays_retryable(self, name):
        """The bridge being down must not park signals -- it restarts often,
        and only reconciliation could release them."""
        from backend.src.services.trading.send_dedup import send_outcome_is_unknown

        assert send_outcome_is_unknown(self._exc(name)) is False

    def test_an_ordinary_rejection_is_still_retryable(self):
        """Control: the change must not turn every failure into unknown."""
        from backend.src.services.trading.send_dedup import send_outcome_is_unknown

        assert send_outcome_is_unknown(ValueError("Invalid stops")) is False
        assert send_outcome_is_unknown(RuntimeError("retcode 10016")) is False
