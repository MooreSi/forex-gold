"""The Mac's Telegram channel set-up reaches the VPS: slots, parser config, keywords.

Owner, 2026-09-29: the VPS "receives the telegram signals from the source and
not the mac but the mac can change the parsing, risk settings etc. that apply
to the telegram signals", and the Mac's channel list and per-channel parser
settings are the master copy. Spec: docs/todo/010.

Found that day: nothing carried these across. The Mac listened to three
channels and the VPS to two (Gold Diggers Scalping was Mac-only); per-channel
`channel_parser_config` (enabled, prefix, IME) and the Logic Keywords
lexicons (which fire CLOSE ALL and market orders) were per node. Risk
settings, channel strategy, learned rules and EA templates already synced.

Same shape as the EA templates (sync/_ea_templates_sync.py): the Mac sends
the whole set on connect, on any change and every 10 minutes, and the VPS
writes what differs. The VPS answers with what it did, so a slot it could not
start listening on is reported on the Mac rather than found as missed trades.

Nothing here reaches Telegram, MetaTrader or a socket: the reader and the
socket are recorders, and each test has its own empty database.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.db import database as db
from backend.src.services.cluster.sync import _channel_setup_sync as css
from backend.src.services.cluster.sync import protocol as P
from backend.src.services.telegram import keywords


def _cfg(name, fmt="gd2", prefix="", ime=1, enabled=1, notes=""):
    return {"channel_name": name, "parser_format": fmt, "signal_prefix": prefix,
            "instant_entry_enabled": ime, "enabled": enabled, "notes": notes}


class _Reader:
    """Three slots, like reader_common._NUM_SLOTS."""
    def __init__(self, slots=None, can_start=True):
        self.slots = slots or [(None, None), (None, None), (None, None)]
        self.active = [gid is not None for gid, _ in self.slots]
        self.calls = []
        self.can_start = can_start
        self.saved = 0

    def get_status(self):
        return {"slots": [{"slot": i + 1, "group_id": gid, "group_name": name,
                           "listener_active": self.active[i]}
                          for i, (gid, name) in enumerate(self.slots)]}

    async def select_group(self, group_id, group_name, slot=1):
        self.calls.append(("select", slot, group_id))
        self.slots[slot - 1] = (int(group_id), group_name)
        self.active[slot - 1] = False
        return {}

    async def start_listener(self, slot=1):
        self.calls.append(("start", slot))
        if not self.can_start:
            return {"error": "Not connected"}
        self.active[slot - 1] = True
        return {"listener_active": True}

    def save_group_selections(self):
        self.saved += 1


MAC_SLOTS = [{"slot": 1, "group_id": 1608388054, "group_name": "Gold Diggers VIP"},
             {"slot": 2, "group_id": 2616846888, "group_name": "GOLD DIGGERS INSTITUTIONAL"},
             {"slot": 3, "group_id": 4436478487, "group_name": "Gold Diggers Scalping"}]


class TestTheMacsSnapshot:
    def test_it_carries_parser_config_lexicons_and_slots(self, fresh_db):
        db.save_channel_parser_config("Gold Diggers VIP", "format_ab", "PFX", True, True, "n")
        with db.db() as conn:
            conn.execute("INSERT OR REPLACE INTO app_config (key,value) VALUES ('selected_groups',?)",
                         (json.dumps(MAC_SLOTS + [{"slot": 4, "group_id": None, "group_name": None}]),))

        snap = css.snapshot()

        assert snap["parser_configs"] == [_cfg("Gold Diggers VIP", "format_ab", "PFX", 1, 1, "n")]
        assert set(snap["lexicons"]) == set(keywords.get_all_lexicons())
        assert snap["slots"] == MAC_SLOTS  # empty slots are not sent

    def test_a_mac_with_no_telegram_sends_no_slots(self, fresh_db):
        assert css.snapshot()["slots"] == []


class TestTheVpsWritesWhatDiffers:
    def test_parser_config_is_written(self, fresh_db):
        result = css.apply_settings({"parser_configs": [_cfg("A", enabled=0)], "lexicons": {}})
        assert result["parser_configs"] == ["A"]
        assert db.get_channel_parser_config("A")["enabled"] == 0

    def test_an_identical_config_is_not_rewritten(self, fresh_db):
        db.save_channel_parser_config("A", "gd2", "", True, True, "")
        result = css.apply_settings({"parser_configs": [_cfg("A")], "lexicons": {}})
        assert result["parser_configs"] == []

    def test_a_channel_only_the_vps_has_is_kept(self, fresh_db):
        db.save_channel_parser_config("VpsOnly", "gd2", "", True, True, "")
        css.apply_settings({"parser_configs": [_cfg("A")], "lexicons": {}})
        assert db.get_channel_parser_config("VpsOnly") is not None

    def test_a_changed_lexicon_is_written(self, fresh_db):
        result = css.apply_settings({"parser_configs": [],
                                     "lexicons": {"close_all": ["SHUT IT ALL"]}})
        assert result["lexicons"] == ["close_all"]
        assert keywords.get_lexicon("close_all") == ["SHUT IT ALL"]

    def test_an_unchanged_lexicon_is_not(self, fresh_db):
        mine = keywords.get_all_lexicons()
        result = css.apply_settings({"parser_configs": [], "lexicons": mine})
        assert result["lexicons"] == []

    @pytest.mark.parametrize("payload", [
        {"parser_configs": "not a list", "lexicons": {}},
        {"parser_configs": [{"channel_name": ""}], "lexicons": {}},
        {"parser_configs": [_cfg("A") | {"enabled": "yes"}], "lexicons": {}},
        {"parser_configs": [], "lexicons": {"no_such_category": ["X"]}},
        {"parser_configs": [], "lexicons": {"close_all": "CLOSE"}},
    ])
    def test_a_bad_payload_writes_nothing_at_all(self, fresh_db, payload):
        """Off-network input. One bad part refuses the lot, so the VPS is
        never left half Mac, half its own."""
        payload = dict(payload)
        payload["parser_configs"] = (payload["parser_configs"] + [_cfg("Good")]
                                     if isinstance(payload["parser_configs"], list)
                                     else payload["parser_configs"])
        before = keywords.get_all_lexicons()

        result = css.apply_settings(payload)

        assert result["error"]
        assert db.get_channel_parser_config("Good") is None
        assert keywords.get_all_lexicons() == before


class TestTheVpsFollowsTheMacsSlots:
    def test_A_MISSING_CHANNEL_IS_SELECTED_AND_STARTED(self):
        """The 2026-09-29 gap: slot 3 was empty on the VPS."""
        reader = _Reader([(1608388054, "Gold Diggers VIP"),
                          (2616846888, "GOLD DIGGERS INSTITUTIONAL"), (None, None)])

        result = asyncio.run(css.apply_slots(reader, MAC_SLOTS))

        assert reader.calls == [("select", 3, 4436478487), ("start", 3)]
        assert result == {"changed": [3], "errors": []}
        assert reader.saved == 1

    def test_a_matching_slot_is_left_alone(self):
        reader = _Reader([(g["group_id"], g["group_name"]) for g in MAC_SLOTS])
        result = asyncio.run(css.apply_slots(reader, MAC_SLOTS))
        assert reader.calls == [] and result["changed"] == [] and reader.saved == 0

    def test_a_different_channel_in_a_slot_is_replaced(self):
        reader = _Reader([(1608388054, "Gold Diggers VIP"), (999, "Old"),
                          (4436478487, "Gold Diggers Scalping")])
        asyncio.run(css.apply_slots(reader, MAC_SLOTS))
        assert reader.calls == [("select", 2, 2616846888), ("start", 2)]

    def test_a_matching_slot_that_stopped_listening_is_restarted(self):
        reader = _Reader([(g["group_id"], g["group_name"]) for g in MAC_SLOTS])
        reader.active[0] = False
        asyncio.run(css.apply_slots(reader, MAC_SLOTS))
        assert reader.calls == [("start", 1)]

    def test_A_SLOT_THE_MAC_LEAVES_EMPTY_IS_NOT_CLEARED(self):
        """A Mac that never logged in to Telegram sends no slots. Clearing
        the VPS's to match would stop every Telegram trade."""
        reader = _Reader([(1608388054, "Gold Diggers VIP"), (None, None), (None, None)])
        asyncio.run(css.apply_slots(reader, []))
        assert reader.calls == [] and reader.slots[0] == (1608388054, "Gold Diggers VIP")

    def test_a_listener_that_will_not_start_is_reported(self):
        reader = _Reader(can_start=False)
        result = asyncio.run(css.apply_slots(reader, MAC_SLOTS[:1]))
        assert result["errors"] == ["slot 1 (Gold Diggers VIP): Not connected"]
        assert reader.saved == 1  # kept, so the reader's restore starts it on reconnect

    def test_no_reader_is_reported_not_raised(self):
        result = asyncio.run(css.apply_slots(None, MAC_SLOTS))
        assert result["errors"] == ["this node has no Telegram reader"]

    @pytest.mark.parametrize("slots", [
        "nope", [{"slot": 0, "group_id": 1, "group_name": "x"}],
        [{"slot": 1, "group_id": "abc", "group_name": "x"}],
        [{"slot": 1, "group_id": 1, "group_name": ""}],
    ])
    def test_bad_slots_change_nothing(self, slots):
        reader = _Reader()
        result = asyncio.run(css.apply_slots(reader, slots))
        assert reader.calls == [] and result["errors"]


class _Ws:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class TestOverTheLink:
    def test_the_mac_sends_on_connect_then_only_on_change(self, fresh_db):
        cli = type("C", (css.ClientChannelSetupMixin,), {})()
        cli._ws, cli.conn_state = _Ws(), P.CONN_CONNECTED
        cli._channel_setup_sent = (None, 0.0)

        assert asyncio.run(cli._push_channel_setup_if_changed()) is True
        assert asyncio.run(cli._push_channel_setup_if_changed()) is False
        db.save_channel_parser_config("New", "gd2", "", True, True, "")
        assert asyncio.run(cli._push_channel_setup_if_changed()) is True

        assert [m["type"] for m in cli._ws.sent] == [P.MSG_CHANNEL_SETUP] * 2
        assert cli._ws.sent[1]["parser_configs"] == [_cfg("New")]

    def test_the_vps_applies_and_answers(self, fresh_db):
        reader = _Reader()
        srv = type("S", (css.ServerChannelSetupMixin,), {})()
        srv._main_engine = type("E", (), {"_tg_reader": reader})()
        ws = _Ws()
        msg = {"type": P.MSG_CHANNEL_SETUP, "parser_configs": [_cfg("A")],
               "lexicons": {}, "slots": MAC_SLOTS[:1]}

        asyncio.run(srv._handle_channel_setup(ws, msg))

        assert db.get_channel_parser_config("A") is not None
        assert reader.calls == [("select", 1, 1608388054), ("start", 1)]
        assert ws.sent == [{"type": P.MSG_CHANNEL_SETUP_ACK, "parser_configs": ["A"],
                            "lexicons": [], "slots": [1], "errors": []}]

    def test_the_vps_answers_a_refusal(self, fresh_db):
        srv = type("S", (css.ServerChannelSetupMixin,), {})()
        srv._main_engine = None
        ws = _Ws()
        asyncio.run(srv._handle_channel_setup(ws, {"parser_configs": "x", "lexicons": {}, "slots": []}))
        assert ws.sent[0]["errors"] and ws.sent[0]["parser_configs"] == []


class TestWiring:
    def test_the_client_starts_the_loop_when_it_connects(self):
        import inspect
        from backend.src.services.cluster.sync import client

        assert "self._channel_setup_sync_loop()" in inspect.getsource(client.SyncClient._connect_once)
        assert issubclass(client.SyncClient, css.ClientChannelSetupMixin)

    def test_the_client_hands_the_answer_to_its_handler(self):
        from backend.src.services.cluster.sync import client
        cli = client.SyncClient()
        asyncio.run(cli._dispatch({"type": P.MSG_CHANNEL_SETUP_ACK, "parser_configs": [],
                                   "lexicons": [], "slots": [], "errors": ["x"]}))
        assert cli.channel_setup_result["errors"] == ["x"]

    def test_the_server_routes_the_message_to_its_handler(self):
        from backend.src.services.cluster.sync import server
        assert issubclass(server.SyncServer, css.ServerChannelSetupMixin)

    def test_the_dashboard_reads_the_vps_answer(self, monkeypatch):
        from backend.src.controllers import remote_node_controller
        from backend.src.services.cluster.sync import client
        cli = client.SyncClient()
        monkeypatch.setattr(client, "get_instance", lambda: cli)
        assert remote_node_controller.channel_setup_result() == {}
        cli.channel_setup_result = {"errors": ["slot 3: Not connected"]}
        assert remote_node_controller.channel_setup_result() == {"errors": ["slot 3: Not connected"]}
