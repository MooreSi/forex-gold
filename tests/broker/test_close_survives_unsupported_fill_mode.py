"""A close is retried under another filling mode when the broker refuses one.

A user on a broker that does not accept IOC saw every ladder partial fail with
`Partial close failed: 10030` (2026-10-02). retcode 10030 is
TRADE_RETCODE_INVALID_FILL: the mode was wrong, nothing was executed. Opening a
trade already walked the modes; closing and partial-closing hard-coded IOC.

Nothing here reaches a broker: the MetaTrader5 module is a stand-in.
"""
from __future__ import annotations

from types import SimpleNamespace

import mt5_bridge

DONE, INVALID_FILL, REQUOTE = 10009, 10030, 10004
RETURN_, IOC, FOK = 2, 1, 0


class _MT5:
    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = DONE
    ORDER_TYPE_SELL, ORDER_TYPE_BUY = 1, 0
    ORDER_TIME_GTC = 0
    ORDER_FILLING_RETURN, ORDER_FILLING_IOC, ORDER_FILLING_FOK = RETURN_, IOC, FOK

    def __init__(self, accepts, retcode_otherwise=INVALID_FILL, none_first=False):
        self.accepts = set(accepts)
        self.retcode_otherwise = retcode_otherwise
        self.none_first = none_first
        self.sent = []

    def positions_get(self, ticket=None):
        return [SimpleNamespace(symbol="XAUUSD", type=0, volume=0.10)]

    def symbol_info(self, symbol):
        return SimpleNamespace(volume_step=0.01)

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=4000.0, ask=4000.3)

    def last_error(self):
        return (1, "x")

    def order_send(self, request):
        self.sent.append(request["type_filling"])
        if self.none_first:
            return None
        if request["type_filling"] in self.accepts:
            return SimpleNamespace(retcode=DONE, comment="ok")
        return SimpleNamespace(retcode=self.retcode_otherwise, comment="bad")


def _use(monkeypatch, mt5):
    monkeypatch.setattr(mt5_bridge, "mt5", mt5)
    monkeypatch.setattr(mt5_bridge, "_ensure_connected", lambda: True)


def test_partial_close_falls_back_when_ioc_is_refused(monkeypatch):
    mt5 = _MT5(accepts={FOK})
    _use(monkeypatch, mt5)
    out = mt5_bridge._partial_close(111, 0.04)
    assert out.get("success") is True
    assert out["lots_closed"] == 0.04
    assert IOC in mt5.sent and mt5.sent[-1] == FOK


def test_full_close_falls_back_when_ioc_is_refused(monkeypatch):
    mt5 = _MT5(accepts={RETURN_})
    _use(monkeypatch, mt5)
    out = mt5_bridge._close_position(111)
    assert out.get("success") is True
    assert mt5.sent[-1] == RETURN_


def test_ioc_broker_is_unchanged_and_sent_once(monkeypatch):
    mt5 = _MT5(accepts={IOC})
    _use(monkeypatch, mt5)
    assert mt5_bridge._partial_close(111, 0.04).get("success") is True
    assert mt5.sent == [IOC]


def test_all_modes_refused_reports_the_error_after_trying_each_once(monkeypatch):
    mt5 = _MT5(accepts=set())
    _use(monkeypatch, mt5)
    out = mt5_bridge._partial_close(111, 0.04)
    assert out == {"error": "Partial close failed: 10030"}
    assert sorted(mt5.sent) == sorted([IOC, RETURN_, FOK])


def test_other_rejection_is_not_retried(monkeypatch):
    mt5 = _MT5(accepts=set(), retcode_otherwise=REQUOTE)
    _use(monkeypatch, mt5)
    out = mt5_bridge._partial_close(111, 0.04)
    assert "10004" in out["error"]
    assert len(mt5.sent) == 1


def test_lost_response_is_never_resent(monkeypatch):
    # No answer is not a refusal: the close may have executed.
    mt5 = _MT5(accepts={FOK}, none_first=True)
    _use(monkeypatch, mt5)
    out = mt5_bridge._partial_close(111, 0.04)
    assert "error" in out
    assert len(mt5.sent) == 1
