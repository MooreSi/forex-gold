"""MT5 AutoTrading is switched back on when it is found off, and the owner hears
about it (owner, 2026-09-28).

The VPS was restarted from its provider's panel on 2026-09-27 and MT5 came back
with AutoTrading off. The watchdog only re-enabled it on a disconnected ->
connected transition, and a bridge that is connected from its first check never
makes one, so every order the Mac forwarded from 01:19 to at least 08:24 was
rejected ("AutoTrading is disabled"): 21 Reversal Engine executions and 3
Telegram signals, each only a log warning on the Mac.

Now, on every healthy check:
  * trade_allowed False -> try `enable_autotrading` (at most once per
    AUTOTRADING_RETRY_S) and alert once per episode, saying whether it worked;
  * trade_allowed None  -> unknown: a broker that could not be asked has not
    said no, so nothing is toggled (20-trading-safety.md, "three states");
  * trade_allowed True  -> the episode is over; the next one alerts again.
And a forwarded order that MT5 rejects for this reason alerts (throttled).

**Nothing here reaches a broker or MetaTrader.** The bridge is a fake that
records `enable_autotrading` calls and returns canned dicts; Telegram is a
recorder. No order is placed, closed or modified.
"""
from __future__ import annotations

import asyncio
from unittest import mock

from backend.src.services.broker import autotrading_guard as guard
from backend.src.services.broker import watchdog
from backend.src.services.telegram import alerts as telegram_alerts


class _Bridge:
    def __init__(self, health, enable_result=None, raises=False):
        self.health = list(health)
        self.enable_result = enable_result or {"enabled": True, "method": "postmessage_33070"}
        self.raises = raises
        self.enables = 0

    async def get_health(self):
        return self.health.pop(0) if self.health else {"connected": True}

    async def enable_autotrading(self):
        self.enables += 1
        if self.raises:
            raise RuntimeError("win32 boom")
        return self.enable_result


def _state():
    return {"last_restart_at": 0.0, "was_connected": True, "consecutive_fails": 0}


def _run(bridge, state, n, start=10_000.0, step=60.0):
    alerts: list[str] = []

    async def fake_send(msg, **kw):
        alerts.append(msg)

    async def fake_start():
        return True

    async def go():
        clock = iter(start + i * step for i in range(n + 5))
        with mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send), \
             mock.patch.object(watchdog, "monotonic", lambda: next(clock)):
            for _ in range(n):
                await watchdog.bridge_watchdog_check(bridge, state, False, fake_start)
            await asyncio.sleep(0)       # let the alert tasks run
    asyncio.run(go())
    return alerts


OFF = {"connected": True, "trade_allowed": False}
ON = {"connected": True, "trade_allowed": True}
UNKNOWN = {"connected": True, "trade_allowed": None}


class TestTheWatchdogSwitchesItBackOn:
    def test_found_off_while_connected_it_is_enabled_and_announced(self):
        bridge = _Bridge([OFF])

        alerts = _run(bridge, _state(), 1)

        assert bridge.enables == 1
        assert len(alerts) == 1
        assert "switched back on" in alerts[0]

    def test_unknown_is_never_toggled(self):
        bridge = _Bridge([UNKNOWN, {"connected": True}])

        alerts = _run(bridge, _state(), 2)

        assert bridge.enables == 0
        assert alerts == []

    def test_already_on_does_nothing(self):
        bridge = _Bridge([ON, ON])

        assert _run(bridge, _state(), 2) == []
        assert bridge.enables == 0

    def test_a_failed_enable_says_orders_will_be_rejected(self):
        bridge = _Bridge([OFF], enable_result={"enabled": False, "error": "window not found"})

        alerts = _run(bridge, _state(), 1)

        assert "rejected" in alerts[0]
        assert "window not found" in alerts[0]

    def test_an_enable_that_raises_is_reported_not_raised(self):
        bridge = _Bridge([OFF], raises=True)

        alerts = _run(bridge, _state(), 1)

        assert "win32 boom" in alerts[0]

    def test_it_does_not_hammer_the_terminal(self):
        """Each attempt clicks at MT5's window. Still off a minute later is
        not retried until AUTOTRADING_RETRY_S has passed, and alerts once."""
        bridge = _Bridge([OFF, OFF, OFF], enable_result={"enabled": False, "error": "x"})

        alerts = _run(bridge, _state(), 3, step=60.0)

        assert bridge.enables == 1
        assert len(alerts) == 1

    def test_retries_after_the_interval(self):
        bridge = _Bridge([OFF, OFF], enable_result={"enabled": False, "error": "x"})

        _run(bridge, _state(), 2, step=guard.AUTOTRADING_RETRY_S + 1)

        assert bridge.enables == 2

    def test_a_new_episode_alerts_again(self):
        bridge = _Bridge([OFF, ON, OFF])

        alerts = _run(bridge, _state(), 3, step=guard.AUTOTRADING_RETRY_S + 1)

        assert bridge.enables == 2
        assert len(alerts) == 2


class TestARejectedOrderAlerts:
    def _note(self, texts, start=1000.0, step=1.0):
        alerts: list[str] = []

        async def fake_send(msg, **kw):
            alerts.append(msg)

        async def go():
            clock = iter(start + i * step for i in range(len(texts) + 2))
            with mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send), \
                 mock.patch.object(guard, "monotonic", lambda: next(clock)):
                guard._last_rejection_alert = None
                for t in texts:
                    guard.note_order_rejection(t)
                await asyncio.sleep(0)
        asyncio.run(go())
        return alerts

    def test_an_autotrading_rejection_alerts(self):
        alerts = self._note(["MT5 order rejected: AutoTrading is disabled in MetaTrader 5. Click"])

        assert len(alerts) == 1
        assert "AutoTrading" in alerts[0]

    def test_other_rejections_do_not(self):
        assert self._note(["Max open trades reached", "FOREIGN KEY constraint failed"]) == []

    def test_a_burst_alerts_once(self):
        msg = "MT5 order rejected: AutoTrading is disabled in MetaTrader 5."

        assert len(self._note([msg, msg, msg])) == 1

    def test_again_after_the_throttle(self):
        msg = "AutoTrading is disabled in MetaTrader 5."

        alerts = self._note([msg, msg], step=guard.REJECTION_ALERT_EVERY_S + 1)

        assert len(alerts) == 2


class TestTheVpsReportsAForwardedRejection:
    """The VPS's handler for a forwarded order hands MT5's rejection to the
    guard. The runtime's open_trade is a stand-in that raises; no order is
    attempted anywhere."""

    def _run(self, handler, msg_type):
        import json
        from backend.src.services.cluster.sync import server as ss
        from backend.src.services.cluster.sync import protocol as P

        class _Runtime:
            async def open_trade(self, **kw):
                raise RuntimeError("MT5 order rejected: AutoTrading is disabled in MetaTrader 5.")

            async def open_manual_market_order(self, **kw):
                raise RuntimeError("MT5 order rejected: AutoTrading is disabled in MetaTrader 5.")

        class _Ws:
            sent: list = []

            async def send(self, raw):
                self.sent.append(json.loads(raw))

        srv = ss.SyncServer.__new__(ss.SyncServer)
        srv._main_engine = _Runtime()
        seen: list[str] = []
        ws = _Ws()
        with mock.patch.object(guard, "note_order_rejection", seen.append), \
             mock.patch("backend.src.services.cluster.sync_repo.mirror_insert_signal_if_absent",
                        lambda *a, **k: None):
            asyncio.run(getattr(srv, handler)(ws, {"type": getattr(P, msg_type),
                                                   "signal_id": "s1", "direction": "BUY"}))
        return seen, ws.sent

    def test_a_forwarded_signal_order(self):
        seen, sent = self._run("_handle_signal_order", "MSG_SIGNAL_ORDER")

        assert seen and "AutoTrading" in seen[0]
        assert "AutoTrading" in sent[-1]["error"]      # the Mac is still told

    def test_a_forwarded_market_order(self):
        seen, _ = self._run("_handle_market_order", "MSG_MARKET_ORDER")

        assert seen and "AutoTrading" in seen[0]
