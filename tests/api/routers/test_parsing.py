"""The Parsing tab: what the reader sees, and the rules it parses by.

Two behaviours carry real history and both are asserted by name:

* **Clamping happens here, not in the browser.** A match window the UI accepts
  and the engine rejects is a setting the operator believes is on and that does
  nothing.
* **A learned rule is saved before its question is dismissed.** Reverse the
  order and a failed write loses the question and the rule together.

Nothing here posts to Telegram. `telegram_controller.send_message` is the only
outbound call in the whole layer and this router does not expose it —
`test_no_endpoint_here_can_post_to_telegram` asserts that rather than assuming
it.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import parsing as parsing_router


@pytest.fixture
def telegram(monkeypatch):
    state = {
        "status": {"auth_state": "CONNECTED", "slots": []},
        "configured": True,
        "settings": {"auto_execute_signals": 0, "lk_enable_tp_parsing": 1},
        "lexicons": {"close_all": ["close all"]},
        "channels": ["GoldSignals"],
        "parser": {"GoldSignals": {"enabled": True}},
        "messages": ([{"id": 1, "text": "XAUUSD BUY"}], 1),
        "pending": [{"id": 7, "raw_text": "???", "channel_name": "GoldSignals"}],
        "written": [],
        "rules": [],
        "resolved": [],
        "saved_parser": [],
        "lexicon_writes": [],
    }

    async def _status(reader):
        state["written"].append(("get_reader_status", reader))
        return state["status"]

    async def _pending(limit):
        return state["pending"][:limit]

    monkeypatch.setattr(parsing_router.tg_ctl, "get_reader_status", _status)
    monkeypatch.setattr(parsing_router.tg_ctl, "reader_is_configured",
                        lambda s: state["configured"])
    monkeypatch.setattr(parsing_router.tg_ctl, "get_risk_settings",
                        lambda: state["settings"])
    monkeypatch.setattr(parsing_router.tg_ctl, "update_risk_settings",
                        lambda f: state["written"].append(("update_risk_settings", f)))
    monkeypatch.setattr(parsing_router.tg_ctl, "get_all_lexicons",
                        lambda: state["lexicons"])
    monkeypatch.setattr(parsing_router.tg_ctl, "set_lexicon",
                        lambda c, p: state["lexicon_writes"].append((c, p)))
    monkeypatch.setattr(parsing_router.tg_ctl, "get_telegram_channel_names",
                        lambda: state["channels"])
    monkeypatch.setattr(parsing_router.tg_ctl, "get_channel_parser_config",
                        lambda ch: state["parser"].get(ch))
    def _set_enabled(ch, enabled):
        state["saved_parser"].append((ch, enabled))
        state["parser"].setdefault(ch, {})["enabled"] = enabled
        return state["parser"][ch]

    monkeypatch.setattr(parsing_router.tg_ctl, "set_channel_parser_enabled", _set_enabled)
    monkeypatch.setattr(parsing_router.tg_ctl, "fetch_stored_messages",
                        lambda limit: state["messages"])
    monkeypatch.setattr(parsing_router.tg_ctl, "get_pending_unrecognised", _pending)
    monkeypatch.setattr(parsing_router.tg_ctl, "save_channel_learned_rule",
                        lambda ch, rule: state["rules"].append((ch, rule)))
    monkeypatch.setattr(parsing_router.tg_ctl, "update_unrecognised_message",
                        lambda rid, status: state["resolved"].append((rid, status)))
    return state


def _client(make_client, reader=object()):
    return make_client(reader_provider=lambda: reader)


# ── Reading the tab ──────────────────────────────────────────────────────────

def test_the_tab_reads_its_pieces_in_one_call(make_client, telegram):
    body = _client(make_client).get("/api/parsing/state").json()

    assert body["configured"] is True
    assert body["settings"]["lk_enable_tp_parsing"] == 1
    assert body["lexicons"] == {"close_all": ["close all"]}
    assert body["channels"] == [{"name": "GoldSignals", "parser": {"enabled": True}}]


def test_an_install_with_no_reader_reports_unconfigured_rather_than_failing(
    make_client, telegram,
):
    """A node with no Telegram credentials is a normal state, not an error, and
    the tab has to render it to tell the operator what to do about it."""
    telegram["configured"] = False

    body = make_client(reader_provider=lambda: None).get("/api/parsing/state").json()

    assert body["reader"] == {}
    assert body["configured"] is False


def test_a_channel_with_no_parser_config_reports_an_empty_one(make_client, telegram):
    """`None` would make the browser guard every read; `{}` is the same answer
    in a shape the UI can render."""
    telegram["parser"] = {}

    body = _client(make_client).get("/api/parsing/state").json()

    assert body["channels"] == [{"name": "GoldSignals", "parser": {}}]


def test_the_message_feed_is_its_own_endpoint(make_client, telegram):
    """The big payload does not ride on the settings read, which polls."""
    body = _client(make_client).get("/api/parsing/messages?limit=50").json()

    assert body["messages"] == [{"id": 1, "text": "XAUUSD BUY"}]
    assert body["total"] == 1


def test_unrecognised_messages_are_listed(make_client, telegram):
    body = _client(make_client).get("/api/parsing/unrecognised").json()

    assert body["pending"][0]["id"] == 7


# ── Writing settings ─────────────────────────────────────────────────────────

def test_a_switch_writes_only_the_key_it_changed(make_client, telegram):
    """The switches save one at a time. A whole-object write would race the
    poll and put back whatever the last read happened to hold."""
    _client(make_client).put("/api/parsing/settings",
                             json={"immediate_market_entry": 1})

    writes = [f for name, f in telegram["written"] if name == "update_risk_settings"]
    assert writes == [{"immediate_market_entry": 1}]


def test_the_match_window_is_clamped_to_what_the_engine_accepts(make_client, telegram):
    _client(make_client).put(
        "/api/parsing/settings", json={"lk_second_message_match_window_sec": 99_999})

    writes = [f for name, f in telegram["written"] if name == "update_risk_settings"]
    assert writes == [{"lk_second_message_match_window_sec": 3600}]


def test_a_match_window_below_the_floor_is_clamped_up(make_client, telegram):
    _client(make_client).put(
        "/api/parsing/settings", json={"lk_second_message_match_window_sec": 0})

    writes = [f for name, f in telegram["written"] if name == "update_risk_settings"]
    assert writes == [{"lk_second_message_match_window_sec": 1}]


def test_a_value_inside_the_bounds_is_left_alone(make_client, telegram):
    """Negative control: a clamp that always returned a bound would pass the
    two tests above."""
    _client(make_client).put(
        "/api/parsing/settings", json={"lk_second_message_match_window_sec": 300})

    writes = [f for name, f in telegram["written"] if name == "update_risk_settings"]
    assert writes == [{"lk_second_message_match_window_sec": 300}]


def test_the_fallback_stop_distance_is_clamped_too(make_client, telegram):
    _client(make_client).put("/api/parsing/settings", json={"lk_fallback_sl_pips": 5000})

    writes = [f for name, f in telegram["written"] if name == "update_risk_settings"]
    assert writes == [{"lk_fallback_sl_pips": 1000.0}]


def test_a_lexicon_is_written_with_the_phrases_it_was_given(make_client, telegram):
    _client(make_client).put("/api/parsing/lexicon",
                             json={"category": "close_all", "phrases": ["close all", "shut it"]})

    assert telegram["lexicon_writes"] == [("close_all", ["close all", "shut it"])]


def test_toggling_a_channel_forwards_to_the_controller_and_answers_the_new_row(
    make_client, telegram,
):
    """One controller call, and the row it wrote comes back.

    This test used to fake `save_channel_parser_config` with a two-argument
    lambda and assert the merged dict the router passed it. That function takes
    SIX arguments, so what the test described was a function nobody had
    written, and the real endpoint raised TypeError on every click while this
    stayed green (owner report, 2026-09-21). Merging the row is now the
    service's job and is pinned against the real table in
    tests/services/channels/test_parser_enable_toggle.py; what is left here is
    what a router is allowed to do -- forward, and return the answer.
    """
    telegram["parser"] = {"GoldSignals": {"enabled": True, "learned": ["entry zone"]}}

    r = _client(make_client).put("/api/parsing/channel-parser",
                                 json={"channel": "GoldSignals", "enabled": False})

    assert telegram["saved_parser"] == [("GoldSignals", False)]
    assert r.json()["enabled"] is False


# ── Teaching the parser ──────────────────────────────────────────────────────

def test_a_learned_rule_is_saved_before_its_question_is_resolved(make_client, telegram):
    """Order matters. Resolve first and a failed rule write loses both."""
    _client(make_client).post("/api/parsing/unrecognised/resolve", json={
        "row_id": 7, "status": "resolved", "channel": "GoldSignals",
        "rule": {"pattern": "ENTRY ZONE", "means": "entry"},
    })

    assert telegram["rules"] == [("GoldSignals", {"pattern": "ENTRY ZONE", "means": "entry"})]
    assert telegram["resolved"] == [(7, "resolved")]


def test_dismissing_a_question_teaches_nothing(make_client, telegram):
    """Negative control: a dismissal that saved a rule would teach the parser
    from a message somebody explicitly said was not a signal."""
    _client(make_client).post("/api/parsing/unrecognised/resolve",
                              json={"row_id": 7, "status": "dismissed"})

    assert telegram["rules"] == []
    assert telegram["resolved"] == [(7, "dismissed")]


# ── Safety ───────────────────────────────────────────────────────────────────

def test_no_endpoint_here_can_post_to_telegram(make_client, telegram, monkeypatch):
    """`send_message` is the one function in the layer that leaves the machine.
    The Parsing tab reads and configures; it does not post."""
    posted = []
    monkeypatch.setattr(parsing_router.tg_ctl, "send_message",
                        lambda *a, **k: posted.append((a, k)))

    client = _client(make_client)
    client.get("/api/parsing/state")
    client.get("/api/parsing/messages")
    client.put("/api/parsing/settings", json={"auto_execute_signals": 1})
    client.post("/api/parsing/unrecognised/resolve",
                json={"row_id": 7, "status": "dismissed"})

    assert posted == []
    source = (parsing_router.__file__)
    with open(source, encoding="utf-8") as fh:
        body = "\n".join(l for l in fh if not l.strip().startswith("#"))
    assert "send_message(" not in body
