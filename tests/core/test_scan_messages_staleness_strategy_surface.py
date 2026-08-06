"""Proves forex_trader.core.core_scan_messages_staleness_strategy's
extracted functions behave identically to SimulationEngine's originals,
characterized in test_scan_messages_staleness_strategy_characterization.py
-- see docs/todo/refactor/core-scan-messages-staleness-strategy-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. No MT5 order is ever placed, closed, or modified.
"""
import asyncio
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core import telegram_alerts, ai_provider, channel_strategy_ai
from forex_trader.core import core_scan_messages_staleness_strategy as ss


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db.update_risk_settings({"accept_tg_signals": 1})
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


def _get_tg_row(tg_id):
    with db.db() as conn:
        r = conn.execute("SELECT * FROM vantage_tg_signals WHERE tg_message_id=?", (tg_id,)).fetchone()
        return db.row_to_dict(r) if r else None


_NOW_ISO = datetime.now(timezone.utc).isoformat()
_STALE_ISO = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

_PARSED = {"direction": "BUY", "entry_low": 4529.0, "entry_high": 4534.0, "stop_loss": 4527.0,
           "tp1": 4537.0, "tp2": 4539.0, "tp3": 4541.0, "tp4": 4543.0, "tp5": 4545.0,
           "tp6": None, "tp7": None, "tp8": None}


def _msg(tg_id, timestamp):
    return {"id": tg_id, "group_id": "g1", "text": "XAU USD BUY NOW", "timestamp": timestamp,
           "sender_name": "sender"}


def test_stale_message_recorded_historical_alerted_once(fresh_db):
    alerts = []

    async def fake_send(msg, *a, **k):
        alerts.append(msg)

    with mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send), \
         mock.patch.object(telegram_alerts, "fmt_signal", return_value="stale alert"):
        proceed = asyncio.run(ss.record_staleness_or_new(
            "c1", "g1", "TestChannel", _msg("c1", _STALE_ISO), _PARSED, "TestChannel"))
    assert proceed is False
    row = _get_tg_row("c1")
    assert row["status"] == "historical"
    assert len(alerts) == 1


def test_no_timestamp_treated_as_stale(fresh_db):
    with mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()), \
         mock.patch.object(telegram_alerts, "fmt_signal", return_value="stale alert"):
        proceed = asyncio.run(ss.record_staleness_or_new(
            "c3", "g1", "TestChannel", _msg("c3", ""), _PARSED, "TestChannel"))
    assert proceed is False
    row = _get_tg_row("c3")
    assert row["status"] == "historical"


def test_fresh_message_recorded_new(fresh_db):
    proceed = asyncio.run(ss.record_staleness_or_new(
        "c2", "g1", "TestChannel", _msg("c2", _NOW_ISO), _PARSED, "TestChannel"))
    assert proceed is True
    row = _get_tg_row("c2")
    assert row["status"] == "new"


async def _default_paused():
    return False


def _resolve(rs=None, text="XAU USD BUY NOW", auto_execute=False, ai_configured=False,
            eval_return=None, eval_raises=False, is_paused_fn=None):
    rs = rs or {}

    async def fake_eval(engine, parsed, channel_name, cfg):
        if eval_raises:
            raise RuntimeError("eval boom")
        return eval_return

    with mock.patch.object(ai_provider, "is_configured", return_value=ai_configured), \
         mock.patch.object(channel_strategy_ai, "evaluate_signal_strategy", side_effect=fake_eval):
        return asyncio.run(ss.resolve_strategy_and_skip_reason(
            rs, "TestChannel", text, _PARSED, auto_execute, {}, "engine_sentinel",
            is_paused_fn or _default_paused,
        ))


def test_no_override_uses_global_trade_strategy(fresh_db):
    result = _resolve(rs={"trade_strategy": "be_runner"})
    assert result["strategy_name"] == "Breakeven Runner"


def test_auto_override_auto_execute_off_uses_channel_rec(fresh_db):
    db.set_channel_strategy_override("TestChannel", None, auto=True)
    db.set_channel_strategy_rec("TestChannel", "trail_stop", "reasoning", 0.9)
    result = _resolve(auto_execute=False)
    assert result["strategy_name"] == "Trailing Stop"


def test_auto_override_ai_not_configured_uses_channel_rec(fresh_db):
    db.set_channel_strategy_override("TestChannel", None, auto=True)
    db.set_channel_strategy_rec("TestChannel", "protected_scale", "reasoning", 0.9)
    result = _resolve(auto_execute=True, ai_configured=False)
    assert result["strategy_name"] == "Protected Scale"


def test_auto_override_ai_eval_skip(fresh_db):
    db.set_channel_strategy_override("TestChannel", None, auto=True)
    result = _resolve(auto_execute=True, ai_configured=True,
                      eval_return={"skip": True, "reasoning": "too risky"})
    assert result["per_signal_skip"] is True
    assert result["per_signal_skip_reason"] == "too risky"


def test_auto_override_ai_eval_strategy(fresh_db):
    db.set_channel_strategy_override("TestChannel", None, auto=True)
    result = _resolve(auto_execute=True, ai_configured=True,
                      eval_return={"skip": False, "strategy": "no_sl_scale", "confidence": 0.8})
    assert result["strategy_name"] == "Trend Ratchet"


def test_auto_override_ai_eval_raises_falls_back_to_channel_rec(fresh_db):
    db.set_channel_strategy_override("TestChannel", None, auto=True)
    db.set_channel_strategy_rec("TestChannel", "conservative_trial", "reasoning", 0.9)
    result = _resolve(auto_execute=True, ai_configured=True, eval_raises=True)
    assert result["strategy_name"] == "Conservative Trial"


def test_specific_channel_override_used_directly(fresh_db):
    db.set_channel_strategy_override("TestChannel", "trail_stop", auto=False)
    result = _resolve()
    assert result["strategy_name"] == "Trailing Stop"


def test_high_risk_text_forces_conservative(fresh_db):
    db.set_channel_strategy_override("TestChannel", "trail_stop", auto=False)
    result = _resolve(text="XAU USD BUY NOW\nHigh Risk Trade")
    assert result["strategy_name"] == "Conservative"


def test_dpm_enabled_overrides_displayed_name_only(fresh_db):
    result = _resolve(rs={"dpm_enabled": 1})
    assert result["strategy_name"] == "DPM"


def test_session_not_allowed_skip_reason(fresh_db):
    with mock.patch.object(db, "is_session_allowed", return_value=(False, "asian")):
        result = _resolve()
    assert "Asian Market Turned Off" in result["skip_reason"]
    assert result["sess_ok"] is False


def test_trading_paused_skip_reason(fresh_db):
    db.set_app_config("risk_halt_reason", "daily loss limit hit")
    db.set_app_config("trade_pause_until", str(time.time() + 3600))

    async def paused():
        return True

    result = _resolve(is_paused_fn=paused)
    assert "daily loss limit hit" in result["skip_reason"]


def test_default_skip_reason(fresh_db):
    result = _resolve()
    assert result["skip_reason"] == "Auto-execution is OFF — activate manually in the dashboard."


# ── Trading Schedule per-window override (2026-08-06) ────────────────────
# core_signal_resolution has honoured the active window's own strategy/
# template pick since the feature landed; this path never did, so a template
# assigned per schedule window was silently ignored for every Telegram signal
# arriving through the scan loop.

def _schedule_today(override="", channel="TestChannel", channel_enabled=True):
    from datetime import datetime
    from forex_trader.core import core_trading_schedule as sched
    schedule = sched._default_schedule()
    day = sched.DAY_NAMES[datetime.now().weekday()]
    schedule[day][0] = {
        "enabled": True, "start": "00:00", "end": "23:59", "target": 0.0,
        "telegram_default_enabled": True,
        "telegram_channels": {
            db._canonical(channel): {
                "enabled": channel_enabled, "strategy_override": override,
            },
        },
    }
    return schedule


def _enable_schedule(**kw):
    from forex_trader.core import core_trading_schedule as sched
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_schedule_today(**kw))


def test_schedule_window_template_beats_the_channel_strategy(fresh_db):
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Sched Tpl", {"mode": "grid"})
    db.set_channel_strategy_override("TestChannel", "trail_stop")
    _enable_schedule(override="template:Sched Tpl")
    result = _resolve()
    assert result["strategy"] == "template:Sched Tpl"
    assert result["strategy_name"] == "Template: Sched Tpl"


def test_schedule_window_template_beats_a_different_channel_template(fresh_db):
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Channel Tpl", {"mode": "single"})
    et.save_ea_template("Window Tpl", {"mode": "grid"})
    db.set_channel_strategy_override("TestChannel", "template:Channel Tpl")
    _enable_schedule(override="template:Window Tpl")
    assert _resolve()["strategy"] == "template:Window Tpl"


def test_schedule_window_plain_strategy_override_also_applies(fresh_db):
    db.set_channel_strategy_override("TestChannel", "trail_stop")
    _enable_schedule(override="conservative")
    assert _resolve()["strategy"] == "conservative"


def test_window_with_no_override_leaves_the_channel_pick_alone(fresh_db):
    db.set_channel_strategy_override("TestChannel", "trail_stop")
    _enable_schedule(override="")
    assert _resolve()["strategy"] == "trail_stop"


def test_schedule_disabled_leaves_the_channel_pick_alone(fresh_db):
    from forex_trader.core import core_trading_schedule as sched
    db.set_channel_strategy_override("TestChannel", "trail_stop")
    sched.set_trading_schedule(_schedule_today(override="conservative"))
    sched.set_trading_schedule_enabled(False)
    assert _resolve()["strategy"] == "trail_stop"


def test_channel_disabled_in_the_window_leaves_the_channel_pick_alone(fresh_db):
    # get_schedule_strategy_override returns None for a source the window has
    # switched off -- "no opinion", not "use this". Blocking is a separate gate.
    db.set_channel_strategy_override("TestChannel", "trail_stop")
    _enable_schedule(override="conservative", channel_enabled=False)
    assert _resolve()["strategy"] == "trail_stop"


def test_a_schedule_assigned_template_survives_the_high_risk_clobber(fresh_db):
    # "HIGH RISK TRADE" is channel-wide boilerplate on GOLD DIGGERS
    # INSTITUTIONAL, and the guard that stops it overriding a template reads
    # the already-resolved strategy -- so the schedule override has to land
    # before it, not after.
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Sched Tpl", {"mode": "grid"})
    _enable_schedule(override="template:Sched Tpl")
    result = _resolve(text="HIGH RISK TRADE — XAU USD BUY NOW")
    assert result["strategy"] == "template:Sched Tpl"


def test_high_risk_still_clobbers_a_schedule_assigned_plain_strategy(fresh_db):
    # Unchanged behaviour for non-template overrides.
    _enable_schedule(override="trail_stop")
    result = _resolve(text="HIGH RISK TRADE — XAU USD BUY NOW")
    assert result["strategy"] == "conservative"
