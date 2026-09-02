"""How the admin is asked to approve a new machine, and how admins stay in step.

A new machine registering produces a Telegram message with inline buttons: one
Approve per licence duration, and a Reject. Pressing one of those grants or
refuses a licence, so what the message carries is what the admin acts on.

Two failures matter:

  * **Buttons for the wrong token.** They carry an eight-character prefix, and
    that prefix is what the approval handler resolves back to a machine. A
    message built without one, or with the wrong one, approves nothing or --
    worse -- the wrong thing.
  * **The notify taking down the registration.** It runs on the registration
    path. A Telegram outage must cost the notification, not the request; the
    machine is still queued and can be approved from the console.

And the two admin-push helpers, which keep every open admin console showing
the same list. A console that missed a push shows a machine as pending after
it was approved, and the obvious next action is to approve it again.

No Telegram and no sockets: every send is captured.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.cluster.remote import server as rs
from backend.src.services.cluster.remote.protocol import (
    MSG_CLIENTS_PUSH, MSG_PENDING_PUSH,
)

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self, fails=False):
        self.sent: list = []
        self.fails = fails

    async def send(self, raw):
        if self.fails:
            raise ConnectionResetError("gone")
        self.sent.append(json.loads(raw))

    def types(self):
        return [m.get("type") for m in self.sent]


@pytest.fixture
def sent(monkeypatch):
    """Capture the Telegram alert instead of sending it."""
    calls: list = []

    async def _send(text, *a, **kw):
        calls.append((text, kw.get("reply_markup")))
        return True
    monkeypatch.setattr(
        "backend.src.services.telegram.alerts.send_message", _send)
    return calls


@pytest.fixture
def admins(monkeypatch):
    monkeypatch.setattr(rs, "_admin_clients", {})
    monkeypatch.setattr(rs, "_pending", {})
    return rs._admin_clients


class TestTheRegistrationAlert:
    async def test_it_carries_the_machine_details(self, sent):
        await rs._notify_new_registration("mac-01", "a@b.c", "Simon's Mac",
                                          "1.2.3.4", token="abcdef0123456789")

        text = sent[0][0]
        for expected in ("mac-01", "a@b.c", "Simon's Mac", "1.2.3.4"):
            assert expected in text

    async def test_missing_details_show_a_placeholder(self, sent):
        """An empty line reads as a rendering fault; a dash reads as "not
        given", which is what it is."""
        await rs._notify_new_registration("", "", "", "", token="abcdef0123456789")

        assert "—" in sent[0][0]

    async def test_the_durations_on_offer_are_the_expected_ones(self):
        """Pinned explicitly rather than derived from _REG_DURATIONS.

        The test below checks the buttons MATCH the list -- so if a duration
        were dropped, both sides would shrink together and it would still
        pass, while the admin silently lost the ability to grant that licence
        length. Mutation found exactly that.
        """
        assert [c for c, _l in rs._REG_DURATIONS] == ["6m", "1y", "2y", "3y", "perp"]

    async def test_there_is_an_approve_button_for_every_duration(self, sent):
        await rs._notify_new_registration("h", "e", "n", "i",
                                          token="abcdef0123456789")

        markup = sent[0][1]
        labels = [b["text"] for row in markup["inline_keyboard"] for b in row]
        for _code, label in rs._REG_DURATIONS:
            assert any(label in l for l in labels), label

    async def test_there_is_a_reject_button(self, sent):
        await rs._notify_new_registration("h", "e", "n", "i",
                                          token="abcdef0123456789")

        labels = [b["text"] for row in sent[0][1]["inline_keyboard"] for b in row]

        assert any("Reject" in l for l in labels)

    async def test_every_button_carries_this_token(self, sent):
        """The prefix is what the approval handler resolves back to a
        machine. A button carrying the wrong one approves the wrong machine."""
        await rs._notify_new_registration("h", "e", "n", "i",
                                          token="abcdef0123456789")

        blob = json.dumps(sent[0][1])

        assert "abcdef01" in blob
        assert "23456789" not in blob, "the full token went into the buttons"

    async def test_no_token_means_no_buttons(self, sent):
        """There is nothing for a button to act on. Offering one that cannot
        resolve is worse than offering none."""
        await rs._notify_new_registration("h", "e", "n", "i", token="")

        assert sent[0][1] is None


class TestTheAlertCannotBreakRegistration:
    async def test_a_telegram_failure_is_swallowed(self, monkeypatch):
        """It runs on the registration path. The machine is queued either
        way and can still be approved from the console."""
        async def _boom(*a, **kw):
            raise RuntimeError("telegram down")
        monkeypatch.setattr(
            "backend.src.services.telegram.alerts.send_message", _boom)

        await rs._notify_new_registration("h", "e", "n", "i", token="abcdef01")

    async def test_a_missing_button_helper_is_swallowed(self, monkeypatch, sent):
        monkeypatch.setattr(
            "backend.src.services.positions.core_bot_panel._btn",
            lambda *a: (_ for _ in ()).throw(RuntimeError("no panel")))

        await rs._notify_new_registration("h", "e", "n", "i", token="abcdef01")


class TestKeepingEveryAdminConsoleInStep:
    async def test_the_pending_list_reaches_every_admin(self, admins):
        a, b = _Ws(), _Ws()
        admins.update({"a": {"ws": a}, "b": {"ws": b}})
        rs._pending.update({"tok": {"hostname": "mac-01"}})

        await rs._push_pending_to_all_admins()

        assert MSG_PENDING_PUSH in a.types() and MSG_PENDING_PUSH in b.types()

    async def test_the_push_carries_the_tokens_alongside_the_items(self, admins):
        """The console needs the token to approve; the item alone is a name."""
        admins.update({"a": {"ws": _Ws()}})
        rs._pending.update({"tok-1": {"hostname": "mac-01"}})

        await rs._push_pending_to_all_admins()

        msg = admins["a"]["ws"].sent[0]
        assert msg["tokens"] == ["tok-1"]
        assert msg["items"] == [{"hostname": "mac-01"}]

    async def test_a_dead_admin_console_is_dropped(self, admins):
        admins.update({"dead": {"ws": _Ws(fails=True)}})

        await rs._push_pending_to_all_admins()

        assert "dead" not in admins

    async def test_one_dead_console_does_not_stop_the_others(self, admins):
        """The failure that matters: a console left showing a stale pending
        list will approve a machine that was already approved."""
        alive = _Ws()
        admins.update({"dead": {"ws": _Ws(fails=True)}, "live": {"ws": alive}})

        await rs._push_pending_to_all_admins()

        assert alive.sent

    async def test_a_live_console_is_kept(self, admins):
        """Negative control: dropping everyone would satisfy the test above."""
        admins.update({"live": {"ws": _Ws()}})

        await rs._push_pending_to_all_admins()

        assert "live" in admins

    async def test_the_client_list_reaches_every_admin(self, admins, monkeypatch):
        monkeypatch.setattr(rs, "get_all_clients", lambda: [{"name": "mac"}])
        a = _Ws()
        admins.update({"a": {"ws": a}})

        await rs._push_clients_to_all_admins()

        assert MSG_CLIENTS_PUSH in a.types()

    async def test_a_dead_console_is_dropped_on_the_client_push_too(
        self, admins, monkeypatch,
    ):
        monkeypatch.setattr(rs, "get_all_clients", lambda: [])
        admins.update({"dead": {"ws": _Ws(fails=True)}})

        await rs._push_clients_to_all_admins()

        assert "dead" not in admins

    @pytest.mark.parametrize("push", ["_push_pending_to_all_admins",
                                      "_push_clients_to_all_admins"])
    async def test_no_admins_connected_is_not_an_error(self, admins, push):
        await getattr(rs, push)()
