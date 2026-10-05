"""A trading pause (daily goal, daily loss, give-back, manual) must stop the
resting-order routes too, not only `open_trade`.

2026-10-05, owner: the VPS badge read "Trading Paused until 05 Oct 22:00" with
the daily goal reached, and trades kept opening. The pause is enforced in one
place, `open_trade`. Three routes reach the broker without it:

  * the Limit Runner (`handle_limit_order_signal`), including its Entry
    Realignment market fallback;
  * the Reversal Engine's LIMIT ORDER path (`_try_re_limit_order`);
  * orders already resting at the broker when the halt lands, which MT5 fills
    with no round trip back to Python.

Manual orders (channel "Manual": manual limit, Set & Forget, ORB) are exempt:
the owner wants those to keep working through a pause.

Nothing here reaches a broker. The EA is a recording fake.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.services.broker import ea_bridge as ea_mod
from backend.src.services.reversal_engine import reversal_engine_repo as re_db
from backend.src.services.reversal_engine.reversal_engine_live_execute import _LiveExecuteMixin
from backend.src.services.signals import resolution
from backend.src.services.trading import limit_order_signal as los
from backend.src.services.trading import resting_revalidation as rr


def _pause(on: bool = True):
    until = time.time() + 3600 if on else 0
    db.set_app_config("trade_pause_until", str(until))
    db.set_app_config("risk_halt_reason", "Daily goal secured: +$30.00" if on else "")


class _EA:
    def __init__(self):
        self.placed = []
        self.cancelled = []

    def is_ea_healthy(self):
        return True

    async def place_pending_order(self, *a, **kw):
        self.placed.append((a, kw))
        return {"type": "pending_order_placed", "ticket": 4242}

    async def cancel_pending_order(self, trade_id, ticket, reason):
        self.cancelled.append((trade_id, ticket, reason))
        return True


async def _balance():
    return 1000.0


def _lot(entry, sl, balance, risk_pct):
    return 0.02


def _parsed():
    return {
        "direction": "BUY", "entry_low": 4122.0, "entry_high": 4127.0,
        "stop_loss": 4118.0, "tp1": 4131.0, "tp2": 4135.0, "tp3": None,
        "tp4": None, "tp5": None, "tp6": None, "tp7": None, "tp8": None,
        "tp_open": False,
    }


async def _limit_runner(ea, monkeypatch):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals (tg_message_id,group_id,raw_text,parsed_at,status) "
            "VALUES (?,?,?,?,?)", ("31099", "g1", "raw", 0.0, "new"))
    monkeypatch.setattr(ea_mod, "get_instance", lambda: ea)
    return await los.handle_limit_order_signal(
        _parsed(), "31099", "GOLD DIGGERS INSTITUTIONAL", "GOLD DIGGERS INSTITUTIONAL",
        {"risk_per_trade_pct": 0.5, "strategy_lot_size": 0},
        sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
        skip_reason="",
        get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot,
    )


class TestLimitRunner:
    def test_paused_places_nothing(self, fresh_db, monkeypatch):
        _pause()
        ea = _EA()
        result = asyncio.run(_limit_runner(ea, monkeypatch))
        assert ea.placed == []
        assert "paused" in result["skip_reason"].lower()
        assert "Daily goal secured" in result["skip_reason"]

    def test_paused_blocks_the_realignment_market_fallback_too(self, fresh_db, monkeypatch):
        _pause()
        ea = _EA()
        opened = []

        async def _no(*a, **kw):
            opened.append(a)
            return {}
        monkeypatch.setattr(los, "_open_realigned_market_order", _no)
        asyncio.run(_limit_runner(ea, monkeypatch))
        assert opened == [] and ea.placed == []

    def test_not_paused_still_places(self, fresh_db, monkeypatch):
        _pause(False)
        ea = _EA()
        asyncio.run(_limit_runner(ea, monkeypatch))
        assert len(ea.placed) == 1


class TestReversalEngineLimit:
    def test_paused_places_nothing_and_does_not_claim_the_signal(self, fresh_db, monkeypatch):
        _pause()
        ea = _EA()
        claimed, statuses = [], []
        monkeypatch.setattr(ea_mod, "get_instance", lambda: ea)
        monkeypatch.setattr(re_db, "claim_vantage_signal_activation",
                            lambda sid: claimed.append(sid) or 1)
        monkeypatch.setattr(re_db, "update_live_exec",
                            lambda _id, status=None, **k: statuses.append(status))

        async def _resolved(*a, **k):
            raise AssertionError("must refuse before resolving a trade")
        monkeypatch.setattr(resolution, "resolve_open_trade_params", _resolved)
        mixin = _LiveExecuteMixin()
        mixin._bridge = None
        out = asyncio.run(mixin._try_re_limit_order({"id": 1, "signal_ref": "RE-1"}, "v1", None))
        assert out is True
        assert ea.placed == [] and claimed == []
        assert statuses and "paused" in statuses[0].lower()


def _row(trade_id, channel="GOLD DIGGERS INSTITUTIONAL", status="working", ticket=900):
    return {"trade_id": trade_id, "ea_ticket": ticket, "direction": "BUY",
            "status": status, "channel_name": channel, "price": 4127.0,
            "stop_loss": 4118.0, "lot_size": 0.02, "tps_json": "{}",
            "pcts_json": "[]", "be_at_pos": 0, "created_at": time.time()}


class TestRestingOrdersAreWithdrawnWhilePaused:
    def test_a_paused_book_is_taken_off_every_cycle(self, fresh_db):
        _pause()
        ea = _EA()
        rows = [_row("a", ticket=1), _row("b", channel="Reversal Engine", ticket=2)]
        n = asyncio.run(rr.enforce_trading_pause(ea, {}, fetch=lambda: rows))
        assert n == 2
        assert sorted(c[0] for c in ea.cancelled) == ["a", "b"]
        assert all("paused" in c[2].lower() for c in ea.cancelled)

    def test_manual_orders_are_left_alone(self, fresh_db):
        _pause()
        ea = _EA()
        rows = [_row("m", channel="Manual", ticket=3)]
        assert asyncio.run(rr.enforce_trading_pause(ea, {}, fetch=lambda: rows)) == 0
        assert ea.cancelled == []

    def test_not_paused_withdraws_nothing(self, fresh_db):
        _pause(False)
        ea = _EA()
        rows = [_row("a")]
        assert asyncio.run(rr.enforce_trading_pause(ea, {}, fetch=lambda: rows)) == 0
        assert ea.cancelled == []

    def test_an_already_withdrawn_order_is_not_cancelled_again(self, fresh_db):
        _pause()
        ea = _EA()
        rows = [_row("a", status="withdrawn")]
        assert asyncio.run(rr.enforce_trading_pause(ea, {}, fetch=lambda: rows)) == 0
        assert ea.cancelled == []

    def test_the_60s_sweep_also_withdraws_while_paused(self, fresh_db):
        _pause()
        ea = _EA()
        rows = [_row("a")]
        # Revalidation switched off: the pause must still apply.
        asyncio.run(rr.revalidate_resting_orders(
            ea, {"resting_revalidation_enabled": 0}, bias=None, fetch=lambda: rows))
        asyncio.run(rr.enforce_trading_pause(ea, {}, fetch=lambda: rows))
        assert [c[0] for c in ea.cancelled] == ["a"]

    def test_a_withdrawn_order_stays_off_while_paused(self, fresh_db):
        _pause()
        ea = _EA()
        row = _row("a", status="withdrawn")
        reason = rr._refusal_for(row, {}, None, None, None, ignore_proximity=True)
        assert reason and "paused" in reason.lower()

    def test_an_unreadable_pause_withdraws_nothing(self, fresh_db, monkeypatch):
        """Pulling every order because a database read blipped is a
        self-inflicted outage (the module's own fail-open rule)."""
        from backend.src.services.risk import app_config_repo

        def _boom(_k):
            raise RuntimeError("locked")
        monkeypatch.setattr(app_config_repo, "read_app_config_strict", _boom)
        ea = _EA()
        assert asyncio.run(rr.enforce_trading_pause(ea, {}, fetch=lambda: [_row("a")])) == 0
        assert ea.cancelled == []
