"""Alerts that could not be sent during an outage are reported when it ends
(bugs/050).

2026-09-07: seventeen ea_close_unverified alerts failed with "nodename nor
servname provided" -- the machine had lost its network, which is also why
the broker could not confirm the close. The alert built for that situation
is the one guaranteed not to arrive in it, and nothing said so afterwards.

Now: a send that fails for a transport reason is remembered, and the next
send that succeeds is followed by one summary naming how many were lost,
when, and the most recent. Payload rejections (a 400) are not remembered:
resending would not help, and bugs/020 already handles those.

Nothing reaches Telegram: the HTTP client is a scripted fake.
"""
from __future__ import annotations

import pytest

from backend.src.services.telegram import alerts

from tests.telegram.test_alert_delivery import _Client, _Response, _install, sent  # noqa: F401


class _Down:
    """Every POST raises, as httpx does with no network."""

    def __init__(self):
        self.posts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.posts += 1
        raise OSError("[Errno 8] nodename nor servname provided, or not known")


@pytest.mark.asyncio
async def test_the_first_send_after_an_outage_is_followed_by_a_summary(monkeypatch, sent):
    _install(monkeypatch, _Down())
    for _ in range(3):
        assert await alerts.send_message("*EA close unverified*\nticket 1", "t1",
                                         "ea_close_unverified") is False
    up = _Client()
    _install(monkeypatch, up)
    assert await alerts.send_message("*Trade Closed*", "t2", "trade_closed") is True
    assert len(up.posts) == 2
    summary = up.posts[1]["text"]
    assert "3 alerts could not be sent" in summary
    assert "EA close unverified" in summary


@pytest.mark.asyncio
async def test_the_summary_is_sent_once(monkeypatch, sent):
    _install(monkeypatch, _Down())
    await alerts.send_message("*A*", None, "a")
    up = _Client()
    _install(monkeypatch, up)
    await alerts.send_message("*B*", None, "b")
    await alerts.send_message("*C*", None, "c")
    assert len(up.posts) == 3          # B, the summary, C


@pytest.mark.asyncio
async def test_no_outage_no_summary(monkeypatch, sent):
    up = _Client()
    _install(monkeypatch, up)
    await alerts.send_message("*B*", None, "b")
    assert len(up.posts) == 1


@pytest.mark.asyncio
async def test_a_rejected_payload_is_not_counted_as_an_outage(monkeypatch, sent):
    _install(monkeypatch, _Client(_Response(400, "Bad Request: chat not found")))
    await alerts.send_message("*A*", None, "a")
    up = _Client()
    _install(monkeypatch, up)
    await alerts.send_message("*B*", None, "b")
    assert len(up.posts) == 1


@pytest.mark.asyncio
async def test_a_failed_summary_keeps_the_count_for_next_time(monkeypatch, sent):
    _install(monkeypatch, _Down())
    await alerts.send_message("*A*", None, "a")
    await alerts.send_message("*B*", None, "b")
    # B goes through, the summary right behind it does not.
    _install(monkeypatch, _Client(_Response(200), _Response(503, "busy")))
    await alerts.send_message("*C*", None, "c")
    up = _Client()
    _install(monkeypatch, up)
    await alerts.send_message("*D*", None, "d")
    assert "2 alerts could not be sent" in up.posts[1]["text"]
