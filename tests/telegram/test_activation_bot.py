"""The Telegram approval loop that runs on the activation screen.

bugs/021's open item. The activation screen is shown by `guard.enforce()`
BEFORE `TradingRuntime.startup()`, so the normal `bot_command_loop` does not
exist there. The registration alert still arrives (it is a plain HTTPS send),
but nothing polls `getUpdates`, so pressing Approve does nothing at all.

This module is the minimal poller that makes that button work, and the whole
point of it is what it REFUSES. The normal panel dispatcher routes `buy` and
`sell` to real market orders; this loop runs in a process that is not
licensed, has no runtime and no engines, and must never be able to reach
them. So it does not filter before calling the big dispatcher -- it never
imports it.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.telegram import activation_bot as ab

CHAT = "12345"
OTHER = "99999"


def _cbq(data: str, chat: str = CHAT, cbq_id: str = "cb1") -> dict:
    return {"callback_query": {
        "id": cbq_id,
        "data": data,
        "message": {"message_id": 7, "chat": {"id": chat}},
    }}


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {"result": []}
        self.text = text

    def json(self):
        return self._payload


class _Client:
    """Stands in for the pooled httpx client. Records every call."""

    def __init__(self, updates_per_poll):
        self._polls = list(updates_per_poll)
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[dict] = []

    async def get(self, url, params=None, timeout=None):
        self.gets.append(params or {})
        if not self._polls:
            return _Resp(payload={"result": []})
        nxt = self._polls.pop(0)
        if isinstance(nxt, _Resp):
            return nxt
        return _Resp(payload={"result": nxt})

    async def post(self, url, json=None, timeout=None):
        self.posts.append((url.rsplit("/", 1)[-1], json or {}))
        return _Resp()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def posted(self, endpoint):
        return [body for ep, body in self.posts if ep == endpoint]


@pytest.fixture
def bot(monkeypatch):
    """A configured bot, a stopping loop, and captured approvals."""
    approved: list = []
    rejected: list = []
    saved: dict = {}

    monkeypatch.setattr(ab, "_get_telegram_config", lambda: {
        "enabled": True, "bot_token_enc": "TOKEN", "chat_id": CHAT})
    monkeypatch.setattr(ab, "_get_offset", lambda: 0)
    monkeypatch.setattr(ab, "_set_offset", lambda v: saved.update(offset=v))
    monkeypatch.setattr(ab, "_approve", lambda s, d: approved.append((s, d)) or
                        ab.Screen(text="Approved", toast="Approved", mode="edit"))
    monkeypatch.setattr(ab, "_reject", lambda s: rejected.append(s) or
                        ab.Screen(text="Rejected", toast="Rejected", mode="edit"))
    slept: list = []

    async def _no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(ab.asyncio, "sleep", _no_sleep)
    return {"approved": approved, "rejected": rejected, "saved": saved,
            "slept": slept}


def _run(client, polls=1):
    """Run the loop for a bounded number of polls."""
    n = {"i": 0}

    def _running():
        n["i"] += 1
        return n["i"] <= polls

    asyncio.run(ab.activation_bot_loop(_running, client_factory=lambda: client))


class TestTheApproveButtonWorks:
    def test_a_registration_tap_is_approved(self, bot):
        client = _Client([[_cbq("p|reg_ap|abcdef01|perp")]])

        _run(client)

        assert bot["approved"] == [("abcdef01", "perp")]

    def test_a_reject_tap_is_rejected(self, bot):
        client = _Client([[_cbq("p|reg_rj|abcdef01")]])

        _run(client)

        assert bot["rejected"] == ["abcdef01"]

    def test_the_button_is_always_answered(self, bot):
        """Telegram spins the button until the callback is acknowledged, so a
        tap that is not answered looks like a hang."""
        client = _Client([[_cbq("p|reg_ap|abcdef01|perp")]])

        _run(client)

        assert client.posted("answerCallbackQuery")

    def test_the_message_is_edited_to_show_the_outcome(self, bot):
        client = _Client([[_cbq("p|reg_ap|abcdef01|perp")]])

        _run(client)

        assert client.posted("editMessageText")


class TestWhatItRefuses:
    """The reason this module exists separately from the panel loop."""

    @pytest.mark.parametrize("action", [
        "buy", "sell", "delp", "sys2", "fa", "fb", "fs", "pt", "ptr",
        "pause", "sset", "st", "bal", "trades", "root",
    ])
    def test_no_other_action_is_dispatched(self, bot, action):
        """Every one of these is a real panel action. `buy` and `sell` place
        market orders. None may run before the app is licensed."""
        client = _Client([[_cbq(f"p|{action}|EURUSD")]])

        _run(client)

        assert bot["approved"] == []
        assert bot["rejected"] == []
        # Refused, explicitly -- not merely "did not approve". A tap that
        # reaches some other handler would still satisfy the two asserts above.
        answers = client.posted("answerCallbackQuery")
        assert answers and "not licensed" in answers[0]["text"].lower()
        assert client.posted("editMessageText") == []

    def test_a_refused_tap_still_gets_an_answer(self, bot):
        """Otherwise the admin sees a button that spins for ever and has no
        idea the app is simply not licensed yet."""
        client = _Client([[_cbq("p|buy|EURUSD")]])

        _run(client)

        answers = client.posted("answerCallbackQuery")
        assert answers
        assert "not licensed" in answers[0]["text"].lower()

    def test_a_refused_tap_does_not_edit_the_message(self, bot):
        client = _Client([[_cbq("p|buy|EURUSD")]])

        _run(client)

        assert client.posted("editMessageText") == []

    def test_a_malformed_approve_is_refused_not_crashed(self, bot):
        """`reg_ap` carries a token prefix AND a duration. With the duration
        missing, indexing it raises inside the poll loop -- which the outer
        handler swallows, so the button appears to do nothing and the log says
        IndexError. Refuse it cleanly instead."""
        client = _Client([[_cbq("p|reg_ap|abcdef01")]])

        _run(client)

        assert bot["approved"] == []
        answers = client.posted("answerCallbackQuery")
        assert answers and "not licensed" in answers[0]["text"].lower()

    def test_callback_data_outside_the_panel_namespace_is_refused(self, bot):
        """Every panel button is namespaced `p|...`. Anything else did not come
        from a keyboard this app sent."""
        client = _Client([[_cbq("x|reg_ap|abcdef01|perp")]])

        _run(client)

        assert bot["approved"] == []

    def test_it_never_imports_the_trading_panel(self):
        """Structural, and deliberately so: the safety property is that the
        dispatcher routing `buy` is not reachable from here, not that we
        remember to check a list before calling it.

        Comments are stripped first -- this module's docstring names
        core_bot_panel while explaining why it does not import it, and a plain
        substring search matches the explanation.
        """
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path(ab.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)

        assert not any("core_bot_panel" in m for m in imported), sorted(imported)
        assert not any("bot_dispatch" in m for m in imported), sorted(imported)


class TestOnlyTheConfiguredChat:
    def test_a_tap_from_another_chat_is_ignored(self, bot):
        """A callback carries its own chat, so it has to be checked on its
        own -- it is not inherited from anything already trusted."""
        client = _Client([[_cbq("p|reg_ap|abcdef01|perp", chat=OTHER)]])

        _run(client)

        assert bot["approved"] == []

    def test_a_foreign_tap_is_not_even_answered(self, bot):
        """Answering confirms the bot is listening to a chat that is not
        allowed to use it."""
        client = _Client([[_cbq("p|reg_ap|abcdef01|perp", chat=OTHER)]])

        _run(client)

        assert client.posts == []


class TestPolling:
    def test_the_offset_advances_past_a_handled_update(self, bot):
        """Otherwise the same tap is replayed on every poll and the machine is
        approved again for ever."""
        upd = _cbq("p|reg_ap|abcdef01|perp")
        upd["update_id"] = 41
        client = _Client([[upd]])

        _run(client)

        assert bot["saved"]["offset"] == 42

    def test_the_offset_is_persisted(self, bot):
        upd = _cbq("p|reg_ap|abcdef01|perp")
        upd["update_id"] = 41
        client = _Client([[upd]])

        _run(client)

        assert "offset" in bot["saved"]

    def test_it_stops_when_told_to(self, bot):
        client = _Client([[], [], []])

        _run(client, polls=2)

        assert len(client.gets) == 2

    def test_it_does_not_poll_when_telegram_is_disabled(self, bot, monkeypatch):
        monkeypatch.setattr(ab, "_get_telegram_config", lambda: {
            "enabled": False, "bot_token_enc": "TOKEN", "chat_id": CHAT})
        client = _Client([[]])

        _run(client)

        assert client.gets == []

    def test_it_does_not_poll_without_a_token(self, bot, monkeypatch):
        monkeypatch.setattr(ab, "_get_telegram_config", lambda: {
            "enabled": True, "bot_token_enc": "", "chat_id": CHAT})
        client = _Client([[]])

        _run(client)

        assert client.gets == []

    def test_a_409_conflict_does_not_kill_the_loop(self, bot):
        """Only one process may long-poll a token. The runtime's own loop
        taking over is the NORMAL end of this one's life, not a crash."""
        client = _Client([_Resp(status=409, text="Conflict"), []])

        _run(client, polls=2)

        assert len(client.gets) == 2

    def test_a_409_backs_off_properly_rather_than_retrying_at_once(self, bot):
        """The distinction matters at handover: the runtime's loop takes the
        token while this one is still up, and a 5s retry would hammer the API
        until the process exits. Pinned because dropping the 409 branch
        entirely still "kept polling" and looked correct."""
        client = _Client([_Resp(status=409, text="Conflict"), []])

        _run(client, polls=2)

        assert ab._CONFLICT_BACKOFF_S in bot["slept"]

    def test_a_transport_error_does_not_kill_the_loop(self, bot):
        """This screen is the only way back in. It must not stop polling
        because one request failed."""
        class _Boom(_Client):
            async def get(self, url, params=None, timeout=None):
                self.gets.append(params or {})
                if len(self.gets) == 1:
                    raise OSError("network down")
                return _Resp(payload={"result": []})

        client = _Boom([[], []])

        _run(client, polls=2)

        assert len(client.gets) == 2


class TestMessagesAreNotCommands:
    def test_a_typed_command_is_ignored(self, bot):
        """No slash command is served here. /panel would need the runtime that
        does not exist on this screen."""
        client = _Client([[{"update_id": 1, "message": {
            "chat": {"id": CHAT}, "text": "/panel"}}]])

        _run(client)

        assert client.posted("sendMessage") == []
