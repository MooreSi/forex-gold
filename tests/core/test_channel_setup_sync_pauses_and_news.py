"""Two more of the Mac's trading switches reach the VPS: channel pauses and the news blackout.

Owner, 2026-09-29, after ticket 2108608418 traded from a channel switched off
on the Mac: "check all other settings are wired in correctly". The audit found
two more that stayed on the node they were set on:

- **A channel paused by hand** (`channel_performance.paused` with
  `manual_override = 1`, from the scorecard's pause button or the bot panel).
  Pausing on the Mac did not stop the VPS trading that channel.
- **The news blackout** (Trading > News: on/off, impact, minutes either
  side). It lives in config.yaml, not the risk row, and only the AI keys of
  config.yaml were synced.

Both travel in the channel set-up message (sync/_channel_setup_sync.py), with
its rules: the Mac is the master copy, the VPS writes only what differs, never
removes what the Mac did not send, and one bad part writes nothing at all.

A third finding is pinned here too: a paused channel was refused only on the
full signal path (resolution.py). Instant entry never read the pause, so a
paused channel still fired instant trades on either node.

Nothing here reaches Telegram, MetaTrader, config.yaml or a socket.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from datetime import datetime, timezone

import pytest

import backend.src.config as cfg_module
from backend.src.db import database as db
from backend.src.services.cluster.sync import _channel_setup_sync as css
from backend.src.services.cluster.sync import protocol as P
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from backend.src.services.signals import decision_log_repo as dl_repo
from backend.src.services.trading import instant_entry
from backend.src.utils import news_calendar
from tests.conftest import remove_db_file

INST = "GOLD DIGGERS INSTITUTIONAL"
BLACKOUT = {"enabled": True, "impact": "high", "minutes_before": 30, "minutes_after": 30}


def _blackout_settings(enabled=True, impact="high", before=30, after=30):
    """What news_calendar.get_blackout_settings() returns (it adds `impacts`)."""
    return {"enabled": enabled, "impact": impact, "impacts": frozenset({"high"}),
            "minutes_before": before, "minutes_after": after}


def _auto_pause(source):
    """A pause the scorecard set, not the user: manual_override stays 0."""
    with db.db() as conn:
        conn.execute("INSERT INTO channel_performance (source, paused, manual_override, updated_at) "
                     "VALUES (?,1,0,0)", (source,))


def _perf(source):
    return db.get_channel_performance_map().get(source)


@pytest.fixture
def yaml_writes(monkeypatch):
    written = []
    monkeypatch.setattr(cfg_module, "save_to_yaml", lambda updates: written.append(dict(updates)))
    monkeypatch.setattr(news_calendar, "get_blackout_settings", lambda: _blackout_settings())
    return written


class TestTheMacsSnapshot:
    def test_a_pause_set_by_hand_is_sent(self, fresh_db, yaml_writes):
        db.set_channel_paused(INST, True)
        assert css.snapshot()["channel_pauses"] == [{"source": INST, "paused": 1}]

    def test_a_resume_set_by_hand_is_sent_too(self, fresh_db, yaml_writes):
        db.set_channel_paused(INST, False)
        assert css.snapshot()["channel_pauses"] == [{"source": INST, "paused": 0}]

    def test_a_pause_the_scorecard_set_is_not(self, fresh_db, yaml_writes):
        """Each node's scorecard pauses from its own results; only the
        user's decision is the Mac's to impose."""
        _auto_pause(INST)
        assert css.snapshot()["channel_pauses"] == []

    def test_the_blackout_is_sent_as_the_calendar_reads_it(self, fresh_db, monkeypatch):
        monkeypatch.setattr(news_calendar, "get_blackout_settings",
                            lambda: _blackout_settings(False, "high_medium", 5, 10))
        assert css.snapshot()["news_blackout"] == {
            "enabled": False, "impact": "high_medium", "minutes_before": 5, "minutes_after": 10}


def _payload(**extra):
    return {"parser_configs": [], "lexicons": {}, **extra}


class TestTheVpsWritesWhatDiffers:
    def test_A_PAUSE_FROM_THE_MAC_STOPS_THE_CHANNEL_ON_THE_VPS(self, fresh_db, yaml_writes):
        """The failure: paused on the Mac, still trading on the VPS."""
        result = css.apply_settings(_payload(channel_pauses=[{"source": INST, "paused": 1}]))

        assert result["channel_pauses"] == [INST]
        assert db.get_channel_lot_mult(INST)[1] is True
        assert _perf(INST)["manual_override"] is True

    def test_a_resume_from_the_mac_lifts_a_vps_auto_pause(self, fresh_db, yaml_writes):
        _auto_pause(INST)
        css.apply_settings(_payload(channel_pauses=[{"source": INST, "paused": 0}]))
        assert db.get_channel_lot_mult(INST)[1] is False

    def test_an_identical_pause_is_not_rewritten(self, fresh_db, yaml_writes):
        db.set_channel_paused(INST, True)
        result = css.apply_settings(_payload(channel_pauses=[{"source": INST, "paused": 1}]))
        assert result["channel_pauses"] == []

    def test_a_pause_only_the_vps_has_is_kept(self, fresh_db, yaml_writes):
        db.set_channel_paused("VpsOnly", True)
        css.apply_settings(_payload(channel_pauses=[{"source": INST, "paused": 1}]))
        assert db.get_channel_lot_mult("VpsOnly")[1] is True

    def test_A_BLACKOUT_THE_VPS_DOES_NOT_HAVE_IS_WRITTEN(self, fresh_db, yaml_writes):
        result = css.apply_settings(_payload(news_blackout=dict(BLACKOUT, enabled=False)))

        assert result["news_blackout"] is True
        assert yaml_writes == [{"news_blackout_enabled": False, "news_blackout_impact": "high",
                                "news_blackout_minutes_before": 30,
                                "news_blackout_minutes_after": 30}]

    def test_an_identical_blackout_is_not_rewritten(self, fresh_db, yaml_writes):
        result = css.apply_settings(_payload(news_blackout=dict(BLACKOUT)))
        assert result["news_blackout"] is False and yaml_writes == []

    def test_a_mac_that_sends_neither_changes_neither(self, fresh_db, yaml_writes):
        """A 0.612 Mac, or anything older: the parts it does not send are left alone."""
        db.set_channel_paused("VpsOnly", True)
        result = css.apply_settings(_payload())
        assert yaml_writes == [] and db.get_channel_lot_mult("VpsOnly")[1] is True
        assert "channel_pauses" not in result and "news_blackout" not in result

    @pytest.mark.parametrize("extra", [
        {"channel_pauses": "not a list"},
        {"channel_pauses": [{"source": "", "paused": 1}]},
        {"channel_pauses": [{"source": INST, "paused": "yes"}]},
        {"channel_pauses": [{"source": INST, "paused": 1.0}]},
        {"news_blackout": "on"},
        {"news_blackout": dict(BLACKOUT, enabled="true")},
        {"news_blackout": dict(BLACKOUT, impact="extreme")},
        {"news_blackout": dict(BLACKOUT, minutes_before=-1)},
        {"news_blackout": dict(BLACKOUT, minutes_after=241)},
        {"news_blackout": dict(BLACKOUT, minutes_before="30")},
        {"news_blackout": {k: v for k, v in BLACKOUT.items() if k != "impact"}},
    ])
    def test_a_bad_part_writes_nothing_at_all(self, fresh_db, yaml_writes, extra):
        good = {"channel_name": "Good", "parser_format": "gd2", "signal_prefix": "",
                "instant_entry_enabled": 1, "enabled": 1, "notes": ""}
        payload = _payload(parser_configs=[good],
                           channel_pauses=[{"source": "AlsoGood", "paused": 1}],
                           news_blackout=dict(BLACKOUT, enabled=False))
        payload.update(extra)

        result = css.apply_settings(payload)

        assert result["error"]
        assert db.get_channel_parser_config("Good") is None
        assert _perf("AlsoGood") is None
        assert yaml_writes == []


class _Ws:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class TestOverTheLink:
    def test_the_vps_answers_with_what_it_wrote(self, fresh_db, yaml_writes):
        srv = type("S", (css.ServerChannelSetupMixin,), {})()
        srv._main_engine = None
        ws = _Ws()

        asyncio.run(srv._handle_channel_setup(ws, {
            "type": P.MSG_CHANNEL_SETUP, "parser_configs": [], "lexicons": {}, "slots": [],
            "channel_pauses": [{"source": INST, "paused": 1}],
            "news_blackout": dict(BLACKOUT, minutes_after=15)}))

        ack = ws.sent[0]
        assert ack["channel_pauses"] == [INST] and ack["news_blackout"] is True
        assert ack["errors"] == []

    def test_the_mac_resends_when_a_pause_changes(self, fresh_db, yaml_writes):
        cli = type("C", (css.ClientChannelSetupMixin,), {})()
        cli._ws, cli.conn_state = _Ws(), P.CONN_CONNECTED
        cli._channel_setup_sent = (None, 0.0)

        asyncio.run(cli._push_channel_setup_if_changed())
        db.set_channel_paused(INST, True)
        assert asyncio.run(cli._push_channel_setup_if_changed()) is True
        assert cli._ws.sent[-1]["channel_pauses"] == [{"source": INST, "paused": 1}]


@pytest.fixture
def both_dbs(fresh_db):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    dl_repo.create_schema()
    db.update_risk_settings({"tg_decision_log_enabled": 1,
                             "accept_tg_signals": 1, "max_open_trades": 5})
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    re_repo.close_db()
    remove_db_file(path)


class _NoPriceBridge:
    """Answers "no price", the first broker call after the pause gate. It
    cannot place anything."""
    async def get_tick(self):
        return None


class TestInstantEntryHonoursAPause:
    """`bridge` is a recorder that answers "no price": reaching it proves the
    gates above it stood down, and a refusal above it never touches one."""

    def _run(self, channel=INST, bridge=None):
        rs = db.get_risk_settings()
        asyncio.run(instant_entry.process_instant_entry(
            msg={"timestamp": datetime.now(timezone.utc).isoformat(), "sender_name": "x"},
            tg_id=str(int(time.time() * 1000)), group_id="g1", channel_name=channel,
            text="XAUUSD BUY NOW", direction="BUY", price=None, rs=rs,
            auto_execute=True, bridge=bridge, dpm_candles=None))
        return dl_repo.rows()

    @pytest.fixture(autouse=True)
    def _gates_open(self, monkeypatch):
        """Every other gate above the pause answers "allowed", so a refusal
        can only be the pause."""
        monkeypatch.setattr(instant_entry, "check_trading_schedule", lambda **kw: (True, ""))
        monkeypatch.setattr(instant_entry, "check_news_blackout", lambda: (True, ""))
        monkeypatch.setattr(instant_entry.db_module, "is_session_allowed", lambda rs: (True, ""))

    def test_A_PAUSED_CHANNEL_FIRES_NO_INSTANT_ENTRY(self, both_dbs):
        db.set_channel_paused(INST, True)
        rows = self._run(bridge=None)
        assert len(rows) == 1 and rows[0]["executed"] == 0
        assert "paused" in rows[0]["skip_reason"]

    def test_the_same_channel_unpaused_gets_past_it(self, both_dbs):
        """Negative control: without it, a refusal could be any other gate."""
        db.set_channel_paused(INST, False)
        rows = self._run(bridge=_NoPriceBridge())
        assert len(rows) == 1 and rows[0]["skip_reason"] == "no live price"
