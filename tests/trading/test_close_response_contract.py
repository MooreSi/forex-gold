"""What `close_position` is allowed to return, and why the frozen path relies on it.

stage3/040 stopped a refused broker close becoming a database close, by
requiring `success` before recording. That guard is only as good as the
contract underneath it: it reads `mt5_res.get("success")`, so a response
carrying NEITHER `success` nor `error` would be treated as a refusal by
`monitor_loop`, and -- in `close_trade.close_trade`, which is on the FROZEN
close path and cannot be reshaped -- would fall through every check and record
a close at the app's own local tick price. That is exactly the phantom-close
shape 040 exists to prevent, in the one function nobody may restructure.

Auditing the four `close_position` call sites on 2026-08-31 found the other
three already correct:

    close_trade.py:118   frozen path -- raises on `error`, raises on
                         `success is False`
    close_trade.py:361   ladder legs -- skips the leg, records nothing
    runtime.py:778       residual close -- alerts, records nothing

So there is no gap to fix. What there was, was an unpinned assumption. These
tests pin it: every branch of the real bridge's `_close_position` returns
exactly one of the two shapes, so the frozen path's checks are exhaustive.
Nobody may quietly add a third shape without this going red.

The bridge is loaded against a stubbed MetaTrader5, the same way
test_send_unknown_state.py does it. No terminal, no account, no order.
"""
from __future__ import annotations

import importlib.util
import sys
import types

import pytest


def _load_bridge():
    sys.modules.setdefault("MetaTrader5", types.ModuleType("MetaTrader5"))
    spec = importlib.util.spec_from_file_location("_mt5_bridge_close_contract",
                                                  "mt5_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Pos:
    symbol = "XAUUSD"
    type = 0
    volume = 0.10


class _Tick:
    bid = 3999.0
    ask = 4000.0


class _Done:
    retcode = 10009
    comment = "done"


class _Refused:
    retcode = 10027          # AutoTrading disabled by client
    comment = "AutoTrading disabled by client"


class _Mt5:
    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = 10009
    ORDER_TIME_GTC = 0
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_IOC = 1

    def __init__(self, positions=(_Pos(),), tick=_Tick(), result=_Done(),
                 raises=False):
        self._positions = positions
        self._tick = tick
        self._result = result
        self._raises = raises

    def positions_get(self, ticket=None):
        if self._raises:
            raise RuntimeError("terminal went away mid-call")
        return self._positions

    def symbol_info_tick(self, *_a):
        return self._tick

    def order_send(self, request):
        return self._result

    def last_error(self):
        return (1, "stubbed")


@pytest.fixture
def bridge(monkeypatch):
    mod = _load_bridge()
    monkeypatch.setattr(mod, "_ensure_connected", lambda: True, raising=False)
    return mod


def _assert_one_shape(res: dict) -> None:
    """The contract, in one place: a close answer is a confirmation or a
    refusal, never neither and never both."""
    assert isinstance(res, dict), f"not a dict: {res!r}"
    has_error = bool(res.get("error"))
    confirmed = res.get("success") is True
    assert has_error != confirmed, (
        f"a close response must carry exactly one of error / success=True, "
        f"got {res!r}. The frozen close path reads `success` and would record "
        f"a database close at its own local tick for anything else."
    )


class TestEveryBranchAnswersInOneOfTwoShapes:

    def test_a_confirmed_close(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "mt5", _Mt5())

        res = bridge._close_position(111)

        _assert_one_shape(res)
        assert res["success"] is True
        assert res["close_price"] == _Tick().bid   # a BUY closes on the bid

    def test_a_refused_close(self, bridge, monkeypatch):
        """AutoTrading off -- the exact condition demo 4 in the runbook uses."""
        monkeypatch.setattr(bridge, "mt5", _Mt5(result=_Refused()))

        res = bridge._close_position(111)

        _assert_one_shape(res)
        assert "10027" in res["error"]
        assert "close_price" not in res, (
            "a refusal must not hand back a price -- that is what got recorded"
        )

    def test_a_lost_response(self, bridge, monkeypatch):
        """order_send returned None: no answer at all. Still a refusal shape,
        never a silent success."""
        monkeypatch.setattr(bridge, "mt5", _Mt5(result=None))

        res = bridge._close_position(111)

        _assert_one_shape(res)

    def test_the_position_is_not_there(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "mt5", _Mt5(positions=()))

        res = bridge._close_position(111)

        _assert_one_shape(res)
        assert "not found" in res["error"]

    def test_no_tick(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "mt5", _Mt5(tick=None))

        res = bridge._close_position(111)

        _assert_one_shape(res)

    def test_the_terminal_raises(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "mt5", _Mt5(raises=True))

        res = bridge._close_position(111)

        _assert_one_shape(res)
        assert "terminal went away" in res["error"]

    def test_not_connected(self, bridge, monkeypatch):
        """Disconnected returns `{"error": _last_error}`, so the contract holds
        only because `_last_error` is never empty when the bridge is down --
        pinned separately below."""
        monkeypatch.setattr(bridge, "_ensure_connected", lambda: False)
        monkeypatch.setattr(bridge, "_last_error", "mt5.login() failed: 134")
        monkeypatch.setattr(bridge, "mt5", _Mt5())

        res = bridge._close_position(111)

        _assert_one_shape(res)


class TestTheDisconnectedBranchCannotProduceAnEmptyError:
    """`_close_position` returns `{"error": _last_error}` verbatim when the
    bridge is down. An EMPTY `_last_error` there would give
    `{"error": ""}` -- falsy error, no success -- which is precisely the
    "neither shape" the frozen close path cannot see. It is safe today only
    because every path that turns the connection off records a reason first.

    That is an invariant of a DIFFERENT function, in a different part of the
    file, with nothing linking the two. Hence this test.
    """

    def test_marking_disconnected_always_records_a_reason(self, bridge):
        bridge._mark_disconnected("terminal closed")

        assert bridge._last_error, "an empty reason breaks _close_position's contract"

    def test_a_failed_connect_records_a_reason(self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "_last_error", "")
        monkeypatch.setattr(bridge, "_MT5_AVAILABLE", False)

        assert bridge._connect() is False
        assert bridge._last_error

    def test_a_failed_connect_with_no_credentials_records_a_reason(
            self, bridge, monkeypatch):
        monkeypatch.setattr(bridge, "_last_error", "")
        monkeypatch.setattr(bridge, "_MT5_AVAILABLE", True)
        monkeypatch.setattr(bridge, "_load_credentials",
                            lambda: (None, None, None, None))

        assert bridge._connect() is False
        assert bridge._last_error


class TestTheContractCheckItselfWorks:
    """Negative control. `_assert_one_shape` is doing all the work above, so a
    version of it that accepted anything would make every test vacuous."""

    @pytest.mark.parametrize("bad", [
        {},                                    # neither
        {"ticket": 111, "close_price": 4000},  # a price and no verdict
        {"success": False},                    # falsy success, no reason given
        {"success": True, "error": "boom"},    # both
    ])
    def test_it_rejects_a_response_that_is_neither_or_both(self, bad):
        with pytest.raises(AssertionError):
            _assert_one_shape(bad)

    def test_it_accepts_the_two_real_shapes(self):
        _assert_one_shape({"success": True, "ticket": 1, "close_price": 4000.0})
        _assert_one_shape({"error": "Position 1 not found"})
