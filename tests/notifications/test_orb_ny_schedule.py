"""The New York ORB's place in the scheduler sweep (2026-10-01).

Off by default (Expert Tunable `orb_ny_mode` = 0), when the London report
keeps its auto-execute exactly as before -- the characterization tests in
tests/core/test_email_scheduler_*.py pin that. On, the London report is
email-only and the New York decision is what auto-execute acts on.

No order is placed here: `orb_auto_execute` is always faked.
"""
import asyncio
from datetime import datetime, timezone
from unittest import mock

from backend.src.db import database as db
from backend.src.services.notifications import email_service
from backend.src.services.notifications import scheduler as sched
from backend.src.services.risk import expert_params as ep

# Monday 2026-07-20, 14:06 UTC: 10:06 in New York, just after the range.
NY_NOW = datetime(2026, 7, 20, 14, 6, tzinfo=timezone.utc).timestamp()
UK_NOW = datetime(2026, 7, 20, 15, 6)


def _run(report, *, ny_mode=1, provider=True, auto_on=1, now=NY_NOW, uk_now=UK_NOW):
    ep.set_params({"orb_ny_mode": ny_mode})
    db.update_risk_settings({"orb_auto_execute_enabled": auto_on})
    db.save_email_config({"smtp_host": "smtp.example.com" if provider else "",
                          "resend_api_key": "", "mailjet_api_key": "",
                          "orb_report_enabled": 0})
    built, placed, london = [], [], []

    async def fake_ny(bridge, now_ts=None):
        built.append(now_ts)
        return report

    async def fake_auto(r, bridge, is_active):
        placed.append(r)

    async def fake_london(bridge):
        london.append(1)
        return {"direction": "bullish"}

    with mock.patch.object(sched.orb_ny, "build_report", fake_ny), \
         mock.patch.object(sched, "orb_auto_execute", fake_auto), \
         mock.patch.object(sched, "build_orb_report", fake_london), \
         mock.patch.object(sched.time, "time", return_value=now), \
         mock.patch.object(email_service, "send_email"):
        asyncio.run(sched.email_scheduler_sweep("bridge", {}, True, uk_now=uk_now,
                                                local_now=datetime(2026, 7, 20, 1, 0)))
    return built, placed, london


def test_the_mode_is_off_by_default(fresh_db):
    assert ep.get("orb_ny_mode") == 0


def test_a_new_york_signal_is_executed_and_decides_the_day(fresh_db):
    report = {"phase": "signal", "direction": "bullish", "stop": 2400.0,
              "target": 2422.6, "current_price": 2411.3}
    built, placed, _ = _run(report)
    assert placed == [report]
    assert db.get_app_config("orb_auto_execute_last") == "2026-07-20"


def test_a_decided_day_without_a_signal_places_nothing_and_stops_looking(fresh_db):
    built, placed, _ = _run({"phase": "done", "direction": "inside",
                             "position_note": "against the trend"})
    assert placed == []
    assert db.get_app_config("orb_auto_execute_last") == "2026-07-20"


def test_watching_places_nothing_and_keeps_looking(fresh_db):
    built, placed, _ = _run({"phase": "watching", "direction": "inside"})
    assert len(built) == 1 and placed == []
    assert db.get_app_config("orb_auto_execute_last") != "2026-07-20"


def test_a_decided_day_is_not_looked_at_again(fresh_db):
    db.set_app_config("orb_auto_execute_last", "2026-07-20")
    built, placed, _ = _run({"phase": "signal", "direction": "bullish"})
    assert built == [] and placed == []


def test_outside_the_new_york_window_the_bridge_is_not_asked(fresh_db):
    early = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc).timestamp()
    built, placed, _ = _run({"phase": "signal", "direction": "bullish"}, now=early)
    assert built == [] and placed == []


def test_it_trades_with_no_email_provider_configured(fresh_db):
    """The London auto-execute sits behind the email-provider check, so a
    node with no mail set up never traded it. The New York one does not."""
    report = {"phase": "signal", "direction": "bearish"}
    _, placed, _ = _run(report, provider=False)
    assert placed == [report]


def test_the_london_report_does_not_trade_while_new_york_mode_is_on(fresh_db):
    _, placed, london = _run({"phase": "watching"},
                             uk_now=datetime(2026, 7, 20, 8, 30))
    assert london == [] and placed == []


def test_off_means_the_new_york_path_never_runs(fresh_db):
    built, placed, london = _run({"phase": "signal", "direction": "bullish"},
                                 ny_mode=0, uk_now=datetime(2026, 7, 20, 8, 30))
    assert built == []
    assert london == [1]          # the London path is untouched


def test_auto_execute_off_means_nothing_runs(fresh_db):
    built, placed, _ = _run({"phase": "signal", "direction": "bullish"}, auto_on=0)
    assert built == [] and placed == []
