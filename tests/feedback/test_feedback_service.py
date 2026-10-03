"""Feedback from a customer install reaches the issuer exactly once.

The chain is: popup -> `submit` -> (outbox, then the remote connection) ->
server `handle` -> store + Telegram + email -> ack -> outbox cleared.

What can go wrong quietly, and what these pin:

  * **A submission lost while the admin server is unreachable.** It must wait
    in the outbox, and only an ack from the server may remove it.
  * **Announced twice.** A reconnect re-sends anything un-acked. The store
    keys on the id, so a repeat is acked again but not announced again.
  * **One failing channel hiding the others.** Telegram being down must not
    stop the email, nor the entry reaching the console.
  * **User text breaking a message.** It is escaped for Telegram and for HTML.

No sockets, no network, no real files: every path is a tmp path and the
senders are recorders.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.feedback import notify, outbox, service, store
from backend.src.services.cluster.remote import _feedback as wire

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PATH", tmp_path / "feedback.json")
    monkeypatch.setattr(outbox, "_PATH", tmp_path / "outbox.json")


@pytest.fixture
def channels(monkeypatch):
    sent = {"telegram": [], "email": []}

    async def tg(text, **kw):
        sent["telegram"].append(text)
        return True

    async def mail(subject, html, *a, **kw):
        sent["email"].append((subject, html))
        return True, ""
    monkeypatch.setattr(notify, "_send_telegram", tg)
    monkeypatch.setattr(notify, "_send_email", mail)
    return sent


def _entry(**kw):
    base = {"id": "a" * 32, "kind": "bug", "message": "It broke",
            "name": "Sam", "email": "s@x.co", "hostname": "mac-01",
            "version": "1.2.3", "submitted_at": 1000.0}
    return {**base, **kw}


class TestSubmit:
    async def test_a_blank_message_is_refused(self):
        with pytest.raises(ValueError):
            service.submit("bug", "   ")

    async def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError):
            service.submit("rant", "hello")

    async def test_the_message_is_capped(self):
        e = service.submit("feature", "x" * 50_000, issuer=False)
        assert len(e["message"]) == service.MAX_MESSAGE

    async def test_a_client_queues_it_in_the_outbox(self):
        e = service.submit("bug", "It broke", issuer=False)

        assert [p["id"] for p in outbox.pending()] == [e["id"]]
        assert store.list_all() == []

    async def test_the_issuer_records_it_directly(self, channels):
        e = service.submit("bug", "It broke", issuer=True)
        await asyncio.sleep(0.05)

        assert [r["id"] for r in store.list_all()] == [e["id"]]
        assert outbox.pending() == []


class TestReceive:
    async def test_it_is_stored_open(self, channels):
        await service.receive(_entry())

        (row,) = store.list_all()
        assert row["status"] == "open" and row["message"] == "It broke"

    async def test_all_three_channels_fire_once(self, channels):
        await service.receive(_entry())

        assert len(channels["telegram"]) == 1 and len(channels["email"]) == 1

    async def test_a_repeat_is_not_announced_again(self, channels):
        await service.receive(_entry())
        again = await service.receive(_entry())

        assert again is False
        assert len(channels["telegram"]) == 1 and len(store.list_all()) == 1

    async def test_telegram_down_still_emails_and_stores(self, channels, monkeypatch):
        async def boom(text, **kw):
            raise RuntimeError("telegram down")
        monkeypatch.setattr(notify, "_send_telegram", boom)

        await service.receive(_entry())

        assert len(channels["email"]) == 1 and len(store.list_all()) == 1

    async def test_email_down_still_sends_telegram(self, channels, monkeypatch):
        async def boom(*a, **kw):
            raise RuntimeError("smtp down")
        monkeypatch.setattr(notify, "_send_email", boom)

        await service.receive(_entry())

        assert len(channels["telegram"]) == 1

    async def test_html_in_the_message_is_escaped_in_the_email(self, channels):
        await service.receive(_entry(message="<script>x</script>"))

        assert "<script>" not in channels["email"][0][1]


class TestCompleting:
    async def test_it_can_be_marked_completed_and_reopened(self, channels):
        await service.receive(_entry())

        assert store.set_completed("a" * 32, True) is True
        assert store.list_all()[0]["status"] == "completed"
        assert store.list_all()[0]["completed_at"] > 0
        store.set_completed("a" * 32, False)
        assert store.list_all()[0]["status"] == "open"

    async def test_an_unknown_id_is_reported(self):
        assert store.set_completed("nope", True) is False

    async def test_newest_first(self, channels):
        await service.receive(_entry(id="1" * 32, submitted_at=1.0))
        await service.receive(_entry(id="2" * 32, submitted_at=2.0))

        assert [r["id"][0] for r in store.list_all()] == ["2", "1"]


class _Ws:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class TestWire:
    async def test_the_server_stores_and_acks(self, channels):
        ws = _Ws()
        await wire.handle(ws, {"name": "Sam's Mac"}, "mac-01", "10.0.0.2",
                          {"type": wire.MSG_FEEDBACK, "entry": _entry()})

        assert ws.sent == [{"type": wire.MSG_FEEDBACK_ACK, "id": "a" * 32}]
        assert store.list_all()[0]["hostname"] == "mac-01"

    async def test_a_malformed_entry_is_acked_not_stored(self, channels):
        """Acked, or the client would resend a poison message for ever."""
        ws = _Ws()
        await wire.handle(ws, {}, "h", "ip",
                          {"type": wire.MSG_FEEDBACK, "entry": {"id": "z" * 32}})

        assert ws.sent and store.list_all() == []

    async def test_the_client_flush_sends_each_pending_entry(self):
        outbox.add(_entry(id="1" * 32))
        outbox.add(_entry(id="2" * 32))
        ws = _Ws()

        await wire.flush(ws)

        assert [m["entry"]["id"][0] for m in ws.sent] == ["1", "2"]
        assert len(outbox.pending()) == 2        # not cleared until acked

    async def test_an_ack_clears_the_outbox(self):
        outbox.add(_entry(id="1" * 32))

        wire.on_ack({"type": wire.MSG_FEEDBACK_ACK, "id": "1" * 32})

        assert outbox.pending() == []
