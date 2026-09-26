"""Where the latency stamps sit on the real paths (docs/todo/006).

Every stamp is placed by a CALLER around a call whose arguments and result are
unchanged -- nothing inside open_trade, open_trade_from_signal or the EA
bridge. What these pin is that each stamp lands on the path it describes and
on no other: an order stamp on a skipped signal would report a delay that
never happened.

No test here reaches a broker: every main engine is a recorder.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.utils import latency_trace as lt

# The Breakout harness: every gate open, every collaborator a stand-in.
from tests.breakout_signal.test_live_execute_kill_switches import (  # noqa: F401
    _SIGNAL, _run as _run_breakout, lab,
)


@pytest.fixture(autouse=True)
def _clean():
    lt.clear()
    yield
    lt.clear()


class TestBreakout:
    def test_an_executed_signal_is_stamped_from_creation_to_order(self, lab):
        main, _ = _run_breakout(lab)

        assert len(main.opened) == 1
        (e,) = lt.entries("engine")
        assert e["key"] == "bo:42"
        assert e["label"] == "Breakout BO-0042"
        assert set(e["stages"]) == {"e1_created", "e2_exec_start", "e3_ordered"}
        # created_at is 30 s before _NOW, which is in the past: a positive wait
        assert lt.gap_ms("bo:42", "e1_created", "e2_exec_start") > 0

    def test_live_off_leaves_no_trace(self, lab):
        lab["risk"]["bo_live_execution"] = 0

        _run_breakout(lab)

        assert lt.entries("engine") == []

    def test_a_blocked_signal_has_no_order_stamp(self, lab):
        lab["schedule"] = (False, "window closed")

        _run_breakout(lab)

        (e,) = lt.entries("engine")
        assert "e3_ordered" not in e["stages"]


def _reversal_engine(main):
    from backend.src.services.reversal_engine.reversal_engine_live_execute import (
        _LiveExecuteMixin)

    class _E(_LiveExecuteMixin):
        def __init__(self):
            self._main_eng = main
            self._bridge = None
    return _E()


@pytest.fixture
def re_statuses(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        "backend.src.services.reversal_engine.reversal_engine_live_execute"
        ".re_db.update_live_exec",
        lambda sig_id, status=None, **kw: seen.__setitem__(sig_id, status))
    return seen


def _run_reversal(engine):
    asyncio.run(engine._try_live_execute(
        {"id": 5, "signal_ref": "RE-5", "direction": "BUY", "created_at": 1.0},
        4050.0, None))


class TestReversal:
    def test_live_off_leaves_no_trace(self, monkeypatch, re_statuses):
        monkeypatch.setattr("backend.src.db.database.get_risk_settings",
                            lambda: {"re_live_execution": 0})

        _run_reversal(_reversal_engine(None))

        assert lt.entries("engine") == []

    def test_a_blocked_signal_is_stamped_without_an_order(self, monkeypatch, re_statuses):
        monkeypatch.setattr("backend.src.db.database.get_risk_settings",
                            lambda: {"re_live_execution": 1, "strategy_lot_size": 0.01})
        monkeypatch.setattr("backend.src.services.risk.schedule.check_trading_schedule",
                            lambda **kw: (False, "closed"))

        _run_reversal(_reversal_engine(None))

        (e,) = lt.entries("engine")
        assert e["key"] == "re:5" and e["label"] == "Reversal RE-5"
        assert set(e["stages"]) == {"e2_exec_start"}

    def test_its_creation_time_is_not_a_delay(self, monkeypatch, re_statuses):
        """A Reversal signal waits for price to reach its zone -- market time,
        sometimes hours. Stamping its creation would fill the "trigger" hop
        with waits that are not latency."""
        monkeypatch.setattr("backend.src.db.database.get_risk_settings",
                            lambda: {"re_live_execution": 1, "strategy_lot_size": 0.01})
        monkeypatch.setattr("backend.src.services.risk.schedule.check_trading_schedule",
                            lambda **kw: (False, "closed"))

        _run_reversal(_reversal_engine(None))

        assert "e1_created" not in lt.entries("engine")[0]["stages"]


# ── Telegram ─────────────────────────────────────────────────────────────────

class _Msg:
    def __init__(self, mid, date):
        self.id = mid
        self.date = date


class _Event:
    def __init__(self, mid, date):
        self.message = _Msg(mid, date)


class TestTheTelegramListener:
    def _reader(self, tmp_path):
        from backend.src.services.telegram.reader import TelegramReader
        r = TelegramReader({"sessions_dir": str(tmp_path)})
        r._event_queue = asyncio.Queue()
        r._group_names[0] = "GOLD VIP"
        return r

    def test_the_post_time_and_channel_are_stamped_on_arrival(self, tmp_path):
        from datetime import datetime, timezone
        r = self._reader(tmp_path)
        posted = datetime.fromtimestamp(lt.time.time() - 2.0, tz=timezone.utc)

        asyncio.run(r._make_message_handler(0)(_Event(77, posted)))

        (e,) = lt.entries("telegram")
        assert e["label"] == "GOLD VIP"
        assert 1500.0 < lt.gap_ms("77", "t0_posted", "t1_arrived") < 3000.0
        assert r._event_queue.qsize() == 1

    def test_a_message_with_no_date_still_arrives(self, tmp_path):
        r = self._reader(tmp_path)

        asyncio.run(r._make_message_handler(0)(_Event(78, None)))

        assert "t0_posted" not in lt.get("78")
        assert r._event_queue.qsize() == 1


class TestTheScanner:
    """t8 is stamped by the scanner after the execution call returns, and only
    when a trade came back."""

    def _scan(self, monkeypatch, fresh_db, executed):
        from tests.core.test_scan_messages_staleness_strategy_characterization import (
            _GD2_FULL, _now_iso, _run)
        from backend.src.services.signals import scan_messages as sm

        fresh_db.update_risk_settings({"auto_execute_signals": 1})

        async def _fake_exec(*a, **kw):
            return {"executed": executed, "exec_lot": 0.1 if executed else None,
                    "exec_price": 4531.0 if executed else None,
                    "trade_result": {"trade_id": "t"} if executed else None,
                    "skip_reason": "" if executed else "blocked", "gap_note": ""}
        monkeypatch.setattr(sm, "_execute_auto_signal_impl", _fake_exec)
        _run([{"id": "501", "group_id": "", "text": _GD2_FULL, "timestamp": _now_iso()}])

    def test_an_executed_signal_gets_its_order_stamp(self, monkeypatch, fresh_db):
        self._scan(monkeypatch, fresh_db, executed=True)

        assert {"t6_scanning", "t7_decided", "t8_ordered"} <= set(lt.get("501"))

    def test_a_declined_signal_does_not(self, monkeypatch, fresh_db):
        self._scan(monkeypatch, fresh_db, executed=False)

        assert "t7_decided" in lt.get("501")
        assert "t8_ordered" not in lt.get("501")
