"""A machine waiting for approval is announced once, not every 15 seconds.

Measured on the owner's Mac, 2026-09-02: **139 registration requests in an
hour, one every 15 seconds, each sending a Telegram message.** Roughly 240
notifications for a single machine waiting to be approved.

The client re-registers on every reconnect, which is correct -- an admin who
missed the first request must still be able to approve after a restart. What
is not correct is announcing it again each time. The queue entry should be
refreshed; the notification should not be re-sent.

Same principle as bugs/020: one condition, one message. A notification that
arrives 240 times is not information.

The exception is a CHANGE. If the operator re-registers with a different
email or nickname, the admin needs the new details -- the whole reason the
entry is overwritten rather than ignored.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.remote import server as rs

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sent(monkeypatch):
    notes: list = []

    async def _notify(**kw):
        notes.append(kw)
    monkeypatch.setattr(rs, "_notify_new_registration", _notify)
    monkeypatch.setattr(rs, "_pending", {})
    monkeypatch.setattr(rs, "_save_pending", lambda: None)
    return notes


async def _queue(token, details):
    """What the MSG_REGISTER branch does: decide, store, announce if news.

    Mirrors the branch rather than calling it, because that branch lives
    inside the websocket handler and needs a live connection. The decision
    itself -- `registration_is_news` -- is pure and is what these assert on;
    a wiring test at the bottom checks the branch actually uses it.

    The `await` matters: the notification is scheduled with create_task, so
    without a yield `sent` is always empty and every assertion here fails on
    working code.
    """
    import asyncio
    import time

    from backend.src.services.cluster.remote._beacon_version import (
        registration_is_news)

    news = registration_is_news(rs._pending.get(token), details)
    rs._pending[token] = {**details, "ts": time.time()}
    if news:
        asyncio.create_task(rs._notify_new_registration(
            hostname=details.get("hostname", ""), email=details.get("email", ""),
            nickname=details.get("nickname", ""), ip=details.get("ip", ""),
            token=token))
    await asyncio.sleep(0)


def _details(email="a@b.c", nickname="Simon's Mac"):
    return {"hostname": "mac-01", "platform": "darwin", "version": "1.2.3",
            "email": email, "nickname": nickname, "ip": "192.168.0.53"}


class TestTheFirstRequest:
    async def test_it_is_announced(self, sent):
        await _queue("tok-1", _details())

        assert len(sent) == 1

    async def test_it_is_queued_for_the_console(self, sent):
        await _queue("tok-1", _details())

        assert "tok-1" in rs._pending

    async def test_the_announcement_carries_the_details(self, sent):
        await _queue("tok-1", _details())

        assert sent[0]["hostname"] == "mac-01"
        assert sent[0]["token"] == "tok-1"


class TestARepeat:
    async def test_it_is_not_announced_again(self, sent):
        """The one that matters. 240 notifications an hour for one machine."""
        for _ in range(20):
            await _queue("tok-1", _details())

        assert len(sent) == 1

    async def test_it_still_refreshes_the_queue_entry(self, sent):
        """The admin console must show a current timestamp, so an operator can
        tell a machine still asking from one that gave up hours ago."""
        await _queue("tok-1", _details())
        first_ts = rs._pending["tok-1"]["ts"]
        rs._pending["tok-1"]["ts"] = first_ts - 1000

        await _queue("tok-1", _details())

        assert rs._pending["tok-1"]["ts"] > first_ts - 1000


class TestAChange:
    async def test_a_new_email_is_announced(self, sent):
        """The admin needs the new details -- the whole reason the entry is
        overwritten rather than ignored."""
        await _queue("tok-1", _details(email="old@b.c"))

        await _queue("tok-1", _details(email="new@b.c"))

        assert len(sent) == 2
        assert sent[1]["email"] == "new@b.c"

    async def test_a_new_nickname_is_announced(self, sent):
        await _queue("tok-1", _details(nickname="Old"))

        await _queue("tok-1", _details(nickname="New"))

        assert len(sent) == 2

    async def test_a_changed_timestamp_alone_is_not_a_change(self, sent):
        """Negative control: `ts` moves on every single request, so comparing
        the whole entry would announce every repeat and fix nothing."""
        await _queue("tok-1", _details())
        rs._pending["tok-1"]["ts"] = 0

        await _queue("tok-1", _details())

        assert len(sent) == 1


class TestTwoMachines:
    async def test_each_is_announced_on_its_own(self, sent):
        """Suppressing by "something is pending" rather than by token would
        hide the second machine entirely."""
        await _queue("tok-1", _details())
        await _queue("tok-2", _details(nickname="Other"))

        assert len(sent) == 2

    async def test_the_same_machine_with_a_NEW_token_is_announced(self, sent):
        """The case that separates "keyed by token" from "keyed by anything
        pending", and it is a real one: delete the token file and the machine
        re-registers with a fresh token and otherwise identical details. The
        admin approves a TOKEN, so the new one has to be announced or it can
        never be approved.

        Mutation found this: with the two machines above carrying different
        nicknames, a global lookup still announced both and the test passed on
        a broken key.
        """
        await _queue("tok-1", _details())

        await _queue("tok-2", _details())          # identical details

        assert len(sent) == 2
        assert sent[1]["token"] == "tok-2"


class TestItIsWiredIn:
    async def test_the_register_branch_uses_it(self):
        """Otherwise the function is correct and unused, and the phone keeps
        buzzing every fifteen seconds."""
        import pathlib

        src = pathlib.Path(rs.__file__).read_text(encoding="utf-8")
        branch = src[src.index("if msg.get(\"type\") == MSG_REGISTER:"):]
        branch = branch[:branch.index("await _close_ws(websocket)")]

        assert "registration_is_news" in branch
        assert "if _news:" in branch
