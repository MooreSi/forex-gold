"""The sender hears back once the owner marks their feedback Completed.

The chain is: popup -> `submit` records the id in this install's sent list ->
on each server ping the install asks "which of these are completed?" -> the
issuer answers from its store -> the install keeps them as notices -> the
header carries them -> the dashboard shows "Feedback noted and completed" ->
Close marks the notice seen.

What can go wrong quietly, and what these pin:

  * **The popup never shows.** An id that never reaches the sent list, or a
    completed answer the client ignores, leaves the sender hearing nothing.
  * **It shows for ever, or twice.** A notice must leave once seen, and a
    second "completed" answer for the same id must not bring it back.
  * **The wrong sender is told.** The issuer answers only for the ids it was
    asked about, and only for those currently Completed.
  * **The owner's own machine is left out.** The issuer records its own
    feedback directly, never over the wire, so it must check its own store.

No sockets, no network, no real files: every path is a tmp path.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.feedback import notify, outbox, sent, service, store
from backend.src.services.cluster.remote import _feedback as wire

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PATH", tmp_path / "feedback.json")
    monkeypatch.setattr(outbox, "_PATH", tmp_path / "outbox.json")
    monkeypatch.setattr(sent, "_PATH", tmp_path / "sent.json")


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    async def ok(*a, **kw):
        return True

    async def mail_ok(*a, **kw):
        return True, ""
    monkeypatch.setattr(notify, "_send_telegram", ok)
    monkeypatch.setattr(notify, "_send_email", mail_ok)


def _entry(**kw):
    base = {"id": "a" * 32, "kind": "bug", "message": "It broke",
            "name": "Sam", "email": "s@x.co", "hostname": "mac-01",
            "version": "1.2.3", "submitted_at": 1000.0}
    return {**base, **kw}


class _Ws:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _asks(ws):
    return [m for m in ws.sent if m["type"] == wire.MSG_FEEDBACK_ASK]


class TestTheSentList:
    async def test_a_client_submission_is_awaiting(self):
        e = service.submit("bug", "It broke", issuer=False)

        assert sent.awaiting_ids() == [e["id"]]
        assert service.completed_notices(issuer=False) == []

    async def test_an_issuer_submission_is_awaiting_too(self):
        e = service.submit("bug", "It broke", issuer=True)
        await asyncio.sleep(0.05)

        assert sent.awaiting_ids() == [e["id"]]


class TestAsking:
    async def test_the_ping_flush_asks_about_awaiting_ids(self):
        sent.add(_entry(id="1" * 32))
        sent.add(_entry(id="2" * 32))
        ws = _Ws()

        await wire.flush(ws)

        (ask,) = _asks(ws)
        assert sorted(ask["ids"]) == ["1" * 32, "2" * 32]

    async def test_nothing_awaiting_asks_nothing(self):
        ws = _Ws()

        await wire.flush(ws)

        assert _asks(ws) == []

    async def test_the_server_answers_only_completed_asked_ids(self):
        await service.receive(_entry(id="1" * 32))
        await service.receive(_entry(id="2" * 32))
        await service.receive(_entry(id="3" * 32))
        store.set_completed("1" * 32, True)
        store.set_completed("3" * 32, True)   # someone else's: not asked about
        ws = _Ws()

        await wire.handle(ws, {}, "mac-01", "10.0.0.2",
                          {"type": wire.MSG_FEEDBACK_ASK,
                           "ids": ["1" * 32, "2" * 32, "9" * 32]})

        assert ws.sent == [{"type": wire.MSG_FEEDBACK_DONE, "ids": ["1" * 32]}]

    async def test_reopened_before_delivery_is_not_reported(self):
        await service.receive(_entry())
        store.set_completed("a" * 32, True)
        store.set_completed("a" * 32, False)
        ws = _Ws()

        await wire.handle(ws, {}, "h", "ip",
                          {"type": wire.MSG_FEEDBACK_ASK, "ids": ["a" * 32]})

        assert ws.sent == [{"type": wire.MSG_FEEDBACK_DONE, "ids": []}]

    async def test_a_garbage_ask_answers_empty(self):
        ws = _Ws()

        await wire.handle(ws, {}, "h", "ip",
                          {"type": wire.MSG_FEEDBACK_ASK, "ids": "nope"})

        assert ws.sent == [{"type": wire.MSG_FEEDBACK_DONE, "ids": []}]

    async def test_the_old_message_still_stores_and_acks(self):
        ws = _Ws()

        await wire.handle(ws, {}, "mac-01", "ip",
                          {"type": wire.MSG_FEEDBACK, "entry": _entry()})

        assert ws.sent == [{"type": wire.MSG_FEEDBACK_ACK, "id": "a" * 32}]


class TestTheClientHearingBack:
    async def test_a_done_answer_becomes_a_notice(self):
        sent.add(_entry(message="Dark mode please", kind="feature"))

        wire.on_message({"type": wire.MSG_FEEDBACK_DONE, "ids": ["a" * 32]})

        (notice,) = service.completed_notices(issuer=False)
        assert notice["id"] == "a" * 32
        assert notice["kind"] == "feature"
        assert notice["message"] == "Dark mode please"
        assert sent.awaiting_ids() == []

    async def test_an_ack_through_on_message_clears_the_outbox(self):
        outbox.add(_entry())

        wire.on_message({"type": wire.MSG_FEEDBACK_ACK, "id": "a" * 32})

        assert outbox.pending() == []

    async def test_a_seen_notice_leaves_and_does_not_come_back(self):
        sent.add(_entry())
        wire.on_message({"type": wire.MSG_FEEDBACK_DONE, "ids": ["a" * 32]})

        assert service.mark_seen("a" * 32) is True
        wire.on_message({"type": wire.MSG_FEEDBACK_DONE, "ids": ["a" * 32]})

        assert service.completed_notices(issuer=False) == []

    async def test_an_id_this_install_never_sent_is_ignored(self):
        wire.on_message({"type": wire.MSG_FEEDBACK_DONE, "ids": ["f" * 32]})

        assert service.completed_notices(issuer=False) == []

    async def test_marking_an_unknown_notice_seen_is_reported(self):
        assert service.mark_seen("nope") is False


class TestTheIssuersOwnFeedback:
    async def test_completing_it_in_the_store_raises_a_notice(self):
        e = service.submit("feature", "Export to CSV", issuer=True)
        await asyncio.sleep(0.05)
        assert service.completed_notices(issuer=True) == []

        store.set_completed(e["id"], True)

        (notice,) = service.completed_notices(issuer=True)
        assert notice["id"] == e["id"] and notice["message"] == "Export to CSV"

    async def test_a_broken_sent_file_gives_no_notices_rather_than_raising(self, tmp_path):
        (tmp_path / "sent.json").write_text("{not json", encoding="utf-8")

        assert service.completed_notices(issuer=False) == []
