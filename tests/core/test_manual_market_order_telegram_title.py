"""The Telegram title a market order is announced under (owner, 2026-09-25).

`open_manual_market_order` had two titles: "Manual Market Order Placed" for
the plain dialog and "ORB/IVB Trade Executed" for EVERY other caller -- so Set
& Forget's Execute button and its Auto scan were both announced as ORB/IVB
trades. The owner: it "should say 'Set & Forget Executed' not ORB/IVB".

Nothing here reaches a broker: the bridge is the characterization suite's fake
and `send_message` is replaced by a recorder.
"""
import asyncio
from unittest.mock import patch

import pytest

from backend.src.runtime import TradingRuntime
from backend.src.services.trading import manual_market_order as mmo

from .test_manual_market_order_characterization import _FakeBridge


@pytest.mark.parametrize("source, title", [
    ("Set & Forget", "*Set & Forget Executed*"),
    ("Set & Forget Auto", "*Set & Forget Executed*"),
    ("manual_market", "*Manual Market Order Placed*"),
    ("ORB/IVB Report", "*ORB/IVB Trade Executed*"),
])
def test_each_source_is_announced_under_its_own_title(source, title):
    assert mmo.telegram_title(source) == title


def test_the_set_and_forget_order_actually_sends_that_title(fresh_db):
    engine = TradingRuntime.__new__(TradingRuntime)
    engine._bridge = _FakeBridge()
    engine._cfg = {}
    sent = []

    async def record(text, *args, **kwargs):
        sent.append(text)

    async def place():
        await TradingRuntime.open_manual_market_order(
            engine, "BUY", stop_loss=2390.0, take_profit=2430.0,
            source_name="Set & Forget Auto")
        await asyncio.sleep(0)          # let the fire-and-forget alert run

    with patch.object(mmo.telegram_alerts, "send_message", record):
        asyncio.run(place())

    assert sent and sent[0].startswith("*Set & Forget Executed*"), sent
    assert "ORB/IVB" not in sent[0]
