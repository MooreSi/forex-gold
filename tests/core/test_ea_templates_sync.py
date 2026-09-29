"""The Mac's EA templates reach the VPS, and stay in step with every edit.

Owner, 2026-09-29: "always sync the templates to the VPS ... if new templates
are created or changed on the local node they should be synced to the vps".

Found that day: the VPS's `ea_trade_templates` table was empty. Its channels
and the Reversal Engine traded `template:30 TP1 SL50 and Trail`, a template
only the Mac had, so 38 trades ran with no partial-close ladder. Templates had
been per-node by design; nothing had ever carried them across.

The Mac sends its whole library: on connect, whenever it changes, and every
10 minutes regardless. The VPS writes only what differs. Templates the Mac
does not send are kept: a template the VPS has and the Mac does not is never
the cause of an order running without its ladder.

Nothing here reaches MetaTrader, a broker or a socket: the socket is a
recorder, and each test has its own empty database.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.broker import ea_templates
from backend.src.services.cluster.sync import _ea_templates_sync as ets
from backend.src.services.cluster.sync import protocol as P

LADDER = {"tp1_pips": 40.0, "tp2_pips": 50.0, "tp1_pct": 55.0, "tp2_pct": 10.0,
          "sl_pips": 50.0, "trail_mode": "step"}


def _updated_at(name):
    from backend.src.services.broker import repo as broker_repo
    return broker_repo.fetch_ea_template(name)["updated_at"]


class TestTheVpsSide:
    def test_an_empty_vps_gets_every_template(self, fresh_db):
        mac = [{"name": "30 TP1 SL50 and Trail", **ea_templates._clean_fields(LADDER)},
               {"name": "Other", **ea_templates._clean_fields({"tp1_pips": 20.0})}]

        result = ets.apply_library(mac)

        assert sorted(result["written"]) == ["30 TP1 SL50 and Trail", "Other"]
        got = ea_templates.get_ea_template("30 TP1 SL50 and Trail")
        assert got["tp1_pct"] == 55.0 and got["trail_mode"] == "step"

    def test_what_the_vps_already_has_is_not_rewritten(self, fresh_db):
        ea_templates.save_ea_template("Same", LADDER)
        before = _updated_at("Same")

        result = ets.apply_library(ets.library_snapshot())

        assert result["written"] == []
        assert _updated_at("Same") == before

    def test_a_changed_template_is_overwritten(self, fresh_db):
        ea_templates.save_ea_template("Edited", LADDER)
        mac = ets.library_snapshot()
        mac[0]["tp1_pct"] = 70.0

        result = ets.apply_library(mac)

        assert result["written"] == ["Edited"]
        assert ea_templates.get_ea_template("Edited")["tp1_pct"] == 70.0

    def test_a_template_only_the_vps_has_is_kept(self, fresh_db):
        ea_templates.save_ea_template("VpsOnly", LADDER)

        ets.apply_library([{"name": "FromMac", **ea_templates._clean_fields({})}])

        assert ea_templates.get_ea_template("VpsOnly") is not None

    def test_a_bad_entry_writes_nothing_at_all(self, fresh_db):
        mac = [{"name": "Good", **ea_templates._clean_fields(LADDER)},
               {"name": "", "tp1_pips": 10.0}]

        result = ets.apply_library(mac)

        assert result["error"]
        assert ea_templates.get_ea_template("Good") is None

    def test_something_that_is_not_a_list_writes_nothing(self, fresh_db):
        assert ets.apply_library({"name": "x"})["error"]
        assert ea_templates.list_ea_templates() == []


class _Ws:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class _Client(ets.ClientEaTemplatesMixin):
    def __init__(self):
        self._ws = _Ws()
        self.conn_state = P.CONN_CONNECTED


class TestTheMacSide:
    def test_it_sends_its_library_the_first_time(self, fresh_db):
        ea_templates.save_ea_template("30 TP1 SL50 and Trail", LADDER)
        c = _Client()

        assert asyncio.run(c._push_ea_templates_if_changed()) is True

        [msg] = c._ws.sent
        assert msg["type"] == P.MSG_EA_TEMPLATES
        assert [t["name"] for t in msg["templates"]] == ["30 TP1 SL50 and Trail"]
        assert msg["templates"][0]["tp1_pct"] == 55.0

    def test_nothing_changed_nothing_sent(self, fresh_db):
        ea_templates.save_ea_template("A", LADDER)
        c = _Client()
        asyncio.run(c._push_ea_templates_if_changed())

        assert asyncio.run(c._push_ea_templates_if_changed()) is False
        assert len(c._ws.sent) == 1

    def test_a_new_template_is_sent(self, fresh_db):
        ea_templates.save_ea_template("A", LADDER)
        c = _Client()
        asyncio.run(c._push_ea_templates_if_changed())

        ea_templates.save_ea_template("B", LADDER)
        asyncio.run(c._push_ea_templates_if_changed())

        assert [t["name"] for t in c._ws.sent[-1]["templates"]] == ["A", "B"]

    def test_an_edited_template_is_sent(self, fresh_db):
        ea_templates.save_ea_template("A", LADDER)
        c = _Client()
        asyncio.run(c._push_ea_templates_if_changed())

        ea_templates.save_ea_template("A", {**LADDER, "tp1_pct": 60.0})
        asyncio.run(c._push_ea_templates_if_changed())

        assert len(c._ws.sent) == 2
        assert c._ws.sent[-1]["templates"][0]["tp1_pct"] == 60.0

    def test_it_resends_unchanged_after_the_resend_interval(self, fresh_db, monkeypatch):
        ea_templates.save_ea_template("A", LADDER)
        c = _Client()
        asyncio.run(c._push_ea_templates_if_changed())
        monkeypatch.setattr(ets, "_RESEND_EVERY_S", 0.0)

        assert asyncio.run(c._push_ea_templates_if_changed()) is True

    def test_not_connected_sends_nothing(self, fresh_db):
        ea_templates.save_ea_template("A", LADDER)
        c = _Client()
        c.conn_state = P.CONN_DISCONNECTED

        assert asyncio.run(c._push_ea_templates_if_changed()) is False
        assert c._ws.sent == []

    def test_what_the_mac_sends_is_what_the_vps_stores(self, fresh_db):
        """Round trip: the incident's own template, sent and applied on an
        empty library, comes out field for field."""
        ea_templates.save_ea_template("30 TP1 SL50 and Trail", LADDER)
        c = _Client()
        asyncio.run(c._push_ea_templates_if_changed())
        sent = c._ws.sent[0]["templates"]
        want = ea_templates.get_ea_template("30 TP1 SL50 and Trail")
        from backend.src.services.broker import repo as broker_repo
        broker_repo.delete_ea_template("30 TP1 SL50 and Trail")

        ets.apply_library(json.loads(json.dumps(sent)))

        got = ea_templates.get_ea_template("30 TP1 SL50 and Trail")
        assert {k: got[k] for k in ea_templates.DEFAULTS} == \
               {k: want[k] for k in ea_templates.DEFAULTS}


class TestWiring:
    def test_the_client_starts_the_loop_when_it_connects(self):
        import inspect
        from backend.src.services.cluster.sync import client

        assert "self._ea_templates_sync_loop()" in inspect.getsource(client.SyncClient._connect_once)
        assert issubclass(client.SyncClient, ets.ClientEaTemplatesMixin)

    def test_the_server_routes_the_message_to_its_handler(self):
        from backend.src.services.cluster.sync import server

        assert issubclass(server.SyncServer, ets.ServerEaTemplatesMixin)

    def test_the_vps_handler_applies_what_arrives(self, fresh_db):
        class _Server(ets.ServerEaTemplatesMixin):
            pass
        msg = {"type": P.MSG_EA_TEMPLATES,
               "templates": [{"name": "T", **ea_templates._clean_fields(LADDER)}]}

        asyncio.run(_Server()._handle_ea_templates(_Ws(), msg))

        assert ea_templates.get_ea_template("T")["tp1_pct"] == 55.0
