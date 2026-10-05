"""The Limit Runner asks the Trading Schedule and the news blackout before it
places a resting order (bugs/064 defect 2).

2026-09-16: 13 pending orders were placed while the day was over its profit
target, because `handle_limit_order_signal` never called
`check_trading_schedule`. One filled two seconds before the withdrawal sweep,
was force-closed for -$27.00, and that loss pulled the day back under the
target. `scan_auto_execute.py` carries the schedule, news and trend gates
together; this route had only the trend gate. It now asks all three, ahead of
both exits (the resting order and the Entry Realignment market fallback).

Nothing here reaches a broker. The EA is a recording fake.
"""
from __future__ import annotations

import asyncio

from backend.src.services.trading import limit_order_signal as los

from tests.trading.test_pause_covers_limit_and_resting_orders import _EA, _limit_runner


def test_schedule_refusal_places_nothing(fresh_db, monkeypatch):
    monkeypatch.setattr(los, "check_trading_schedule",
                        lambda source="telegram": (False, "daily profit target reached"),
                        raising=False)
    ea = _EA()
    result = asyncio.run(_limit_runner(ea, monkeypatch))
    assert ea.placed == []
    assert "Trading Schedule" in result["skip_reason"]
    assert "daily profit target reached" in result["skip_reason"]


def test_schedule_is_asked_with_the_channel_name(fresh_db, monkeypatch):
    seen = []
    monkeypatch.setattr(los, "check_trading_schedule",
                        lambda source="telegram": seen.append(source) or (True, ""),
                        raising=False)
    asyncio.run(_limit_runner(_EA(), monkeypatch))
    assert seen == ["GOLD DIGGERS INSTITUTIONAL"]


def test_schedule_refusal_blocks_the_realignment_fallback_too(fresh_db, monkeypatch):
    monkeypatch.setattr(los, "check_trading_schedule",
                        lambda source="telegram": (False, "outside today's trading schedule"),
                        raising=False)
    opened = []

    async def _no(*a, **kw):
        opened.append(a)
        return {}
    monkeypatch.setattr(los, "_open_realigned_market_order", _no)
    ea = _EA()
    asyncio.run(_limit_runner(ea, monkeypatch))
    assert opened == [] and ea.placed == []


def test_news_blackout_places_nothing(fresh_db, monkeypatch):
    monkeypatch.setattr(los, "check_news_blackout",
                        lambda: (False, "news blackout: NFP"), raising=False)
    ea = _EA()
    result = asyncio.run(_limit_runner(ea, monkeypatch))
    assert ea.placed == []
    assert "news blackout: NFP" in result["skip_reason"]


def test_both_clear_still_places(fresh_db, monkeypatch):
    monkeypatch.setattr(los, "check_trading_schedule",
                        lambda source="telegram": (True, ""), raising=False)
    monkeypatch.setattr(los, "check_news_blackout", lambda: (True, ""), raising=False)
    ea = _EA()
    asyncio.run(_limit_runner(ea, monkeypatch))
    assert len(ea.placed) == 1
