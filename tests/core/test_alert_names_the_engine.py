"""A trade this app's own engine opened must say so, not claim a channel.

Reported on 2026-09-21: "never received a telegram message when the reversal
engine executed its order". The alert WAS sent -- `vantage_telegram_log` has
it as `trade_open ... sent`, and Telegram returned 200 -- but the line naming
where the trade came from read:

    Channel: Reversal Engine

Among a stream of Telegram-channel alerts (107 that day), an execution by the
app's own engine was formatted exactly like a copied signal, so there was
nothing to pick it out by. "Reversal Engine" is not a channel; it is the
biggest single source of trades on this account (207 of them).

`trade_channel_label` already knew some sources are not channels -- it
excluded "Signal Generator" and "Bounce Generator" -- and the list simply
never grew when the engines were renamed and added to.
"""
from __future__ import annotations

import pytest

from backend.src.services.analytics import labels
from backend.src.services.telegram import alerts as ta


def _trade(tg_source):
    return {
        "trade_id": "t1", "direction": "BUY", "mt5_ticket": 2050682687,
        "entry_price": 4376.97, "entry_low": 4376.97, "entry_high": 4376.97,
        "lot_size": 0.1, "stop_loss": 4326.97, "tp1": 4406.97,
        "strategy": "scale_out", "managed_by": "ea", "tg_source": tg_source,
    }


class TestTheAlertNamesTheOrigin:
    def test_an_engine_trade_is_labelled_as_an_engine(self, fresh_db):
        msg = ta.fmt_trade_open(_trade("Reversal Engine"), None, {})

        assert "Engine: Reversal Engine" in msg

    def test_an_engine_trade_is_not_called_a_channel(self, fresh_db):
        """The whole complaint: it looked like a copied Telegram signal."""
        msg = ta.fmt_trade_open(_trade("Reversal Engine"), None, {})

        assert "Channel: Reversal Engine" not in msg

    def test_a_real_channel_is_still_called_a_channel(self, fresh_db):
        msg = ta.fmt_trade_open(_trade("Gold Diggers VIP"), None, {})

        assert "Channel: Gold Diggers VIP" in msg

    def test_the_orb_report_is_an_engine_too(self, fresh_db):
        msg = ta.fmt_trade_open(_trade("ORB/IVB Report (auto)"), None, {})

        assert "Engine:" in msg
        assert "Channel:" not in msg

    def test_a_manual_market_order_claims_neither(self, fresh_db):
        msg = ta.fmt_trade_open(_trade("manual_market"), None, {})

        assert "Channel:" not in msg
        assert "Engine:" not in msg


class TestWhichSourcesAreChannels:
    @pytest.mark.parametrize("source", [
        "Reversal Engine", "Breakout Engine", "Signal Generator",
        "Bounce Generator", "ORB/IVB Report (auto)",
    ])
    def test_an_internal_engine_is_not_a_channel(self, source):
        assert labels.trade_channel_label(source) == ""

    @pytest.mark.parametrize("source", [
        "Gold Diggers VIP", "GOLD DIGGERS INSTITUTIONAL", "Gold Diggers Scalping",
    ])
    def test_a_telegram_channel_still_is_one(self, source):
        assert labels.trade_channel_label(source) == source

    def test_the_instant_prefix_is_still_stripped(self):
        assert labels.trade_channel_label("instant:Gold Diggers VIP") == "Gold Diggers VIP"

    def test_a_channel_named_after_an_engine_is_matched_exactly(self):
        """Substring matching here would silence a real channel. Only the
        exact engine names, plus the ORB report's own prefix."""
        assert labels.trade_channel_label("Reversal Engine Signals") != ""
