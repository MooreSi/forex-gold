"""One close, one Telegram message — whoever wins the race.

`apply_full_close` is a compare-and-set (`WHERE trade_id=? AND status='open'`,
see tests/trading/test_close_idempotency.py) so a second caller changes nothing
in the database. But it returns None, `record_close` never looks at the
rowcount, and it hands every caller a full-looking result dict either way. The
loser of the race therefore believes it closed the trade and sends its own
Telegram alert.

Live, 2026-09-04, ticket 1940612275 (an EA Template trade, SL trailed to
breakeven, closed after TP2). The owner got the close announced twice:

  * the EA saw its stop fire and pushed `trade_closed` -> `_on_trade_closed`
    -> record_close + the "ea_close" alert;
  * in the same seconds the monitor cycle ran `check_sl` on the same row --
    which happens at monitor_cycle.py:210, BEFORE the `managed_by == 'ea'`
    skip twenty lines below it -- found the tick past the stop and the ticket
    already gone at the broker, so `reconcile_sl_hit` fell through its
    "deferring" guard, called record_close too, and sent the alert again.

Reconciliation was fixed for exactly this in 2026-07 by excluding EA-managed
trades from its poll (broker/repo.py's fetch_python_managed_open_trades, and
the comment above it names ticket 1572181515). The SL path never got the same
treatment, and three more callers can race in besides.

The rule this file pins: the caller whose close did not land says nothing. Not
by teaching each site which other site might beat it -- five callers can reach
here (the monitor loop, reconciliation, the EA bridge, a manual close, the
placeholder repair) -- but by having the one place that already knows who won,
the compare-and-set itself, report it.

Nothing here reaches a broker or an order. Every bridge is a fake with canned
positions and deal history; the one real close path exercised writes to a test
database.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime
from backend.src.services.broker import position_sync as ps
from backend.src.services.broker.ea_bridge import _events
from backend.src.services.positions import core_template_placeholder_repair as repair
from backend.src.services.positions import monitor_loop
from backend.src.services.positions.tp_tracking import TPCache
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.trading import close_trade as close_trade_mod
from backend.src.services.trading import trade_repo
from backend.src.services.trading.close_trade import CloseTradeContext, record_close
from tests._fakes import _ReconciliationBridge


# ── shared fixtures/helpers ───────────────────────────────────────────────────

TICKET = 1940612275


class _Bridge(_ReconciliationBridge):
    """The shared read-only broker double, with this trade's numbers."""

    def __init__(self, **over):
        over.setdefault("account", {"balance": 812.99, "equity": 812.99,
                                    "margin_free": 812.99})
        over.setdefault("tick", SimpleNamespace(bid=4437.90, ask=4438.10))
        super().__init__(**over)


class _ClosingBridge(_Bridge):
    """The one path that needs a CONFIRMED broker close before it records one
    (check_profit_close_target, and close_trade's manual close). Records
    nothing at a broker: it answers success and a price, and nothing else."""

    async def close_position(self, ticket):
        return {"success": True, "close_price": 4437.90}


def _insert_trade(trade_id="t-1", status="open", mt5_ticket=TICKET,
                  remaining_lots=0.10, entry_price=4437.90, managed_by="python"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "SELL", 4437.0, 4438.0, 4470.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, managed_by, net_pnl, realised_pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, "SELL", 4437.0, 4438.0, entry_price,
             0.10, remaining_lots, 4470.0, status, time.time(), managed_by, 0.0, 0.0),
        )


@pytest.fixture
def sent(monkeypatch):
    """Every Telegram message the code under test tried to send."""
    messages: list = []

    async def _send(text, trade_id=None, event_type="", reply_markup=None):
        messages.append({"text": text, "trade_id": trade_id, "event_type": event_type})
        return True

    # Every call site reaches the same module object -- whether it imported it
    # at module scope (`_events`, `position_sync`) or inside the function (the
    # placeholder repair) -- so one patch covers all four.
    monkeypatch.setattr(telegram_alerts, "send_message", _send)
    return messages


async def _settle():
    """Let the fire-and-forget alert tasks actually run."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ── 1. the compare-and-set reports who won ────────────────────────────────────

class TestApplyFullCloseSaysWhetherItLanded:
    """Everything below is built on this. The CAS is the only place that
    knows, without a second read that would reintroduce the race."""

    def test_the_first_close_reports_that_it_landed(self, fresh_db):
        _insert_trade()

        won = trade_repo.apply_full_close(
            "t-1", now=1_000_000.0, close_price=4437.90, reason="SL",
            gross_pnl=0.0, realised_total=33.03, net_pnl_total=33.03,
            net_delta=33.03, signal_id="sig-t-1")

        assert won is True

    def test_a_second_close_reports_that_it_did_not(self, fresh_db):
        _insert_trade()

        def _close():
            return trade_repo.apply_full_close(
                "t-1", now=1_000_000.0, close_price=4437.90, reason="SL",
                gross_pnl=0.0, realised_total=33.03, net_pnl_total=33.03,
                net_delta=33.03, signal_id="sig-t-1")

        _close()

        assert _close() is False

    def test_a_close_for_an_unknown_trade_reports_that_it_did_not(self, fresh_db):
        _insert_trade()

        won = trade_repo.apply_full_close(
            "no-such-trade", now=1_000_000.0, close_price=4437.90, reason="SL",
            gross_pnl=0.0, realised_total=0.0, net_pnl_total=0.0,
            net_delta=0.0, signal_id="sig-t-1")

        assert won is False


# ── 2. record_close carries the verdict to its callers ────────────────────────

class TestRecordCloseTellsItsCallerWhetherItWon:
    """The callers cannot ask the database themselves: by the time they read
    it, the winner has already written 'closed' and both would see the same
    thing."""

    def test_the_close_that_landed_is_not_marked_already_closed(self, fresh_db):
        _insert_trade()
        ctx = CloseTradeContext(_Bridge(), tp_cache=TPCache())

        result = asyncio.run(record_close("t-1", 4437.90, "SL", ctx))

        assert result["already_closed"] is False

    def test_the_close_that_lost_the_race_is_marked_already_closed(self, fresh_db):
        _insert_trade()
        ctx = CloseTradeContext(_Bridge(), tp_cache=TPCache())

        asyncio.run(record_close("t-1", 4437.90, "SL", ctx))
        second = asyncio.run(record_close("t-1", 4437.90, "SL", ctx))

        assert second["already_closed"] is True

    def test_the_rest_of_the_result_is_unchanged(self, fresh_db):
        """Four call sites pass this dict straight to fmt_trade_close. Adding
        a key must not take one away."""
        _insert_trade()
        ctx = CloseTradeContext(_Bridge(), tp_cache=TPCache())

        result = asyncio.run(record_close("t-1", 4437.90, "SL", ctx))

        for key in ("trade_id", "close_price", "gross_pnl", "net_pnl", "reason"):
            assert key in result, key


# ── 3. every alert site is silent when its close did not land ─────────────────

def _engine(bridge=None):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = bridge if bridge is not None else _Bridge()
    e._cfg = {"starting_balance": 1000.0}
    e._tp_trigger_cache = TPCache()
    e._scale_out_last_fail = {}
    e._tp_safety_net_last_alert = {}
    e._profit_sound_seq = 0
    e._mt5_sync_missing_streak = {}
    return e


class _CommentaryCtx(CloseTradeContext):
    """A close context whose Telegram notification is a recorder.

    `background_close_commentary` is the close alert for three of the five
    callers -- the SL reconcile, the profit-close target and a manual close --
    and it lives on TradingRuntime, which is pinned at its LOC baseline and
    cannot grow. So the guard sits at the three call sites, and this records
    whether they reached for it."""

    def __init__(self, bridge):
        super().__init__(bridge, tp_cache=TPCache())
        self.announced: list = []

        async def _commentary(trade_id, result, reason, tick):
            self.announced.append({"trade_id": trade_id, "reason": reason})

        async def _profit_sync(trade_id, ticket):
            return None

        self.background_close_commentary = _commentary
        self.schedule_profit_sync = _profit_sync


@pytest.fixture
def recorded_close(monkeypatch):
    """Patches monitor_loop's record_close to report a chosen race outcome.

    The frozen close path is not run here -- these tests are about what the
    CALLER does with the verdict it gets back."""
    def _set(already_closed: bool):
        async def _record_close(trade_id, price, reason, ctx):
            return {"trade_id": trade_id, "close_price": price, "gross_pnl": 0.0,
                    "net_pnl": 33.03, "reason": reason,
                    "already_closed": already_closed}
        monkeypatch.setattr(monitor_loop, "record_close", _record_close)
    return _set


class TestTheSLReconcilesCloseNotification:
    """The path that fired live: `check_sl` sees the tick past the stop and
    the ticket already gone at the broker."""

    def _trade(self):
        return {"trade_id": "t-1", "direction": "SELL", "entry_price": 4437.90,
                "remaining_lots": 0.1, "realised_pnl": 33.03, "mt5_ticket": TICKET,
                "lot_size": 0.1, "stop_loss": 4437.90}

    def _reconcile(self, ctx):
        asyncio.run(monitor_loop.reconcile_sl_hit(
            self._trade(), SimpleNamespace(bid=4437.90, ask=4438.10),
            4437.90, "SL", ctx.bridge, ctx))

    def test_it_says_nothing_when_the_ea_closed_it_first(self, recorded_close):
        recorded_close(already_closed=True)
        ctx = _CommentaryCtx(_Bridge(positions=[]))

        self._reconcile(ctx)

        assert ctx.announced == [], "the loser of the race announced the close anyway"

    def test_it_still_announces_the_close_it_recorded_itself(self, recorded_close):
        """The negative control. Without it the test above would pass against
        a guard that suppressed every close notification there is."""
        recorded_close(already_closed=False)
        ctx = _CommentaryCtx(_Bridge(positions=[]))

        self._reconcile(ctx)

        assert [a["reason"] for a in ctx.announced] == ["SL"]


class TestTheProfitCloseTargetsNotification:
    """Same verdict, same silence -- this one closes at the broker first, so
    it is slower and can lose the race to reconciliation."""

    def _trade(self):
        return {"trade_id": "t-1", "direction": "SELL", "entry_price": 4437.90,
                "remaining_lots": 0.1, "realised_pnl": 100.0, "mt5_ticket": TICKET,
                "lot_size": 0.1, "stop_loss": 4470.0}

    def _run(self, ctx):
        return asyncio.run(monitor_loop.check_profit_close_target(
            self._trade(), SimpleNamespace(bid=4437.90, ask=4438.10),
            50.0, _ClosingBridge(), ctx))

    def test_it_says_nothing_when_the_trade_was_already_closed(self, recorded_close):
        recorded_close(already_closed=True)
        ctx = _CommentaryCtx(_Bridge())

        assert self._run(ctx) is True
        assert ctx.announced == []

    def test_it_still_announces_the_close_it_recorded_itself(self, recorded_close):
        recorded_close(already_closed=False)
        ctx = _CommentaryCtx(_Bridge())

        assert self._run(ctx) is True
        assert [a["reason"] for a in ctx.announced] == ["profit_close_target"]


class TestTheManualClosesNotification:
    """/close and the UI's Close button. Reconciliation exists to settle rows
    the app missed, so it is the one most likely to have been here already."""

    def _run(self, ctx, monkeypatch, already_closed: bool):
        async def _record_close(trade_id, price, reason, ctx_):
            return {"trade_id": trade_id, "close_price": price, "gross_pnl": 0.0,
                    "net_pnl": 33.03, "reason": reason,
                    "already_closed": already_closed}
        monkeypatch.setattr(close_trade_mod, "record_close", _record_close)
        asyncio.run(close_trade_mod.close_trade("t-1", "manual_close", ctx))

    def test_it_says_nothing_when_the_trade_was_already_closed(
            self, fresh_db, monkeypatch):
        _insert_trade()
        ctx = _CommentaryCtx(_ClosingBridge())

        self._run(ctx, monkeypatch, already_closed=True)

        assert ctx.announced == []

    def test_it_still_announces_the_close_it_recorded_itself(
            self, fresh_db, monkeypatch):
        _insert_trade()
        ctx = _CommentaryCtx(_ClosingBridge())

        self._run(ctx, monkeypatch, already_closed=False)

        assert [a["reason"] for a in ctx.announced] == ["manual_close"]


class TestTheEABridgesCloseNotification:
    """The other half of the live duplicate: the EA's own trade_closed."""

    def _node(self, row, record_close_result):
        class _Engine:
            def __init__(self):
                self.record_close_calls: list = []

            async def record_close(self, trade_id, close_price, reason):
                self.record_close_calls.append((trade_id, close_price, reason))
                return dict(record_close_result)

            async def get_mt5_account(self):
                return {"balance": 812.99, "equity": 812.99, "margin_free": 812.99}

            async def schedule_profit_sync(self, trade_id, ticket):
                return None

        class _Node(_events.EventsMixin):
            def __init__(self):
                self._engine = _Engine()
                self._active: dict = {}

            async def _fetch_trade(self, trade_id):
                return row if trade_id == row["trade_id"] else None

        return _Node()

    def _row(self, **over):
        row = {"trade_id": "t-1", "status": "open", "mt5_ticket": TICKET,
               "direction": "SELL", "lot_size": 0.1, "remaining_lots": 0.1,
               "entry_price": 4437.90, "stop_loss": 4437.90, "tp1": 4400.0,
               "strategy": "template:30 TP1 SL50 and Trail",
               "tg_source": "Reversal Engine", "activated_at": 1_757_000_000.0,
               "close_time": 1_757_003_600.0, "net_pnl": 33.03, "realised_pnl": 33.03}
        row.update(over)
        return row

    def test_it_says_nothing_when_the_monitor_loop_closed_it_first(self, sent):
        node = self._node(self._row(), {
            "trade_id": "t-1", "close_price": 4437.90, "gross_pnl": 0.0,
            "net_pnl": 33.03, "reason": "SL", "already_closed": True})

        async def _run():
            await node._on_trade_closed({
                "type": "trade_closed", "trade_id": "t-1", "ticket": TICKET,
                "close_price": 4437.90, "reason": "SL"})
            await _settle()
        asyncio.run(_run())

        assert [m["event_type"] for m in sent] == []

    def test_it_still_announces_the_close_it_recorded_itself(self, sent):
        node = self._node(self._row(), {
            "trade_id": "t-1", "close_price": 4437.90, "gross_pnl": 0.0,
            "net_pnl": 33.03, "reason": "SL", "already_closed": False})

        async def _run():
            await node._on_trade_closed({
                "type": "trade_closed", "trade_id": "t-1", "ticket": TICKET,
                "close_price": 4437.90, "reason": "SL"})
            await _settle()
        asyncio.run(_run())

        assert [m["event_type"] for m in sent] == ["ea_close"]


class TestReconciliationsCloseNotification:
    """The MT5 sync poll. It already excludes EA-managed rows, but a manual
    close or the placeholder repair can still beat it to the same trade."""

    def _drive(self, engine, record_close_result):
        async def _run():
            with mock.patch.object(
                TradingRuntime, "record_close",
                new=mock.AsyncMock(return_value=dict(record_close_result))
            ), mock.patch.object(
                TradingRuntime, "sync_profit", new=mock.AsyncMock()
            ), mock.patch.object(
                TradingRuntime, "_schedule_profit_sync", new=mock.AsyncMock()
            ):
                await TradingRuntime._sync_closed_mt5_positions(engine)
                await _settle()
        asyncio.run(_run())

    def test_it_says_nothing_when_the_trade_was_already_closed(self, fresh_db, sent):
        _insert_trade()
        engine = _engine(_Bridge(positions=[], deal_history=[], position_history=[]))
        engine._mt5_sync_missing_streak = {"t-1": 1}

        self._drive(engine, {"trade_id": "t-1", "close_price": 4437.90,
                             "gross_pnl": 0.0, "net_pnl": 33.03,
                             "reason": "MT5_close", "already_closed": True})

        assert [m["event_type"] for m in sent] == []

    def test_it_still_announces_the_close_it_recorded_itself(self, fresh_db, sent):
        _insert_trade()
        engine = _engine(_Bridge(positions=[], deal_history=[], position_history=[]))
        engine._mt5_sync_missing_streak = {"t-1": 1}

        self._drive(engine, {"trade_id": "t-1", "close_price": 4437.90,
                             "gross_pnl": 0.0, "net_pnl": 33.03,
                             "reason": "MT5_close", "already_closed": False})

        assert [m["event_type"] for m in sent] == ["mt5_sync_MT5_close"]


class TestThePlaceholderRepairsCloseNotification:
    """The fifth caller. It settles rows the other four left open, so it is
    the most likely of all of them to arrive second."""

    def _deals(self):
        return [
            {"position_id": TICKET, "order": TICKET, "entry": 0, "price": 4437.90,
             "time": 1_757_000_000.0, "volume": 0.1, "profit": 0.0},
            {"position_id": TICKET, "entry": 1, "price": 4437.90, "comment": "sl 4437.90",
             "time": 1_757_003_600.0, "volume": 0.1, "profit": 33.03},
        ]

    def _drive(self, monkeypatch, already_closed: bool):
        async def _fake_record_close(trade_id, close_price, reason, ctx):
            return {"trade_id": trade_id, "close_price": close_price,
                    "gross_pnl": 0.0, "net_pnl": 33.03, "reason": reason,
                    "already_closed": already_closed}
        monkeypatch.setattr(close_trade_mod, "record_close", _fake_record_close)

        deals = self._deals()
        with db.db() as conn:
            row = db.row_to_dict(conn.execute(
                "SELECT * FROM vantage_simulated_trades WHERE trade_id='t-1'"
            ).fetchone())

        async def _run():
            await repair._close_from_deals(row, deals[0], deals, _Bridge())
            await _settle()
        asyncio.run(_run())

    def test_it_says_nothing_when_the_trade_was_already_closed(
            self, fresh_db, sent, monkeypatch):
        _insert_trade(mt5_ticket=0, entry_price=0.0)

        self._drive(monkeypatch, already_closed=True)

        assert [m["event_type"] for m in sent] == []

    def test_it_still_announces_the_close_it_recorded_itself(
            self, fresh_db, sent, monkeypatch):
        _insert_trade(mt5_ticket=0, entry_price=0.0)

        self._drive(monkeypatch, already_closed=False)

        assert [m["event_type"] for m in sent] == ["template_placeholder_repair_sl"]
