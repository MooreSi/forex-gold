"""An alert that silently fails is worse than no alert.

Found 2026-09-01 by reading `vantage_telegram_log`: **107 of 6,866 alerts had
been rejected by Telegram**, and the ones failing were the ones that matter
most --

    ea_bridge_lost   Bad Request: can't parse entities: Can't find end of
    tp_safety_net    the entity starting at ...

`ea_bridge_lost` is the notification for the whole of bugs/013 -- the EA has
stopped responding and management has been reclaimed. It had been failing since
at least 2026-08-26, and the owner believed he would be told.

The cause is `parse_mode: "Markdown"` plus an unescaped dynamic value. Telegram
Markdown v1 treats `_ * ` [` as delimiters, and a single underscore is
unbalanced: `ea_bridge_lost`'s text interpolates the strategy name raw, and
`be_runner`, `scalp_runner`, `trail_stop` and `scale_out` all carry exactly one.

`_md_esc` already exists and its docstring names this exact failure. It simply
is not called at every site, which is why the fix here is a DELIVERY guarantee
rather than another per-site escape: the next formatter to forget will be
caught by the transport instead of losing the message.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.telegram import alerts

PARSE_ERROR = json.dumps({
    "ok": False, "error_code": 400,
    "description": "Bad Request: can't parse entities: Can't find end of the "
                   "entity starting at byte offset 42",
})


class _Response:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class _Client:
    """Records every POST and answers from a scripted list."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.posts: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        self.posts.append(json)
        return self.responses.pop(0) if self.responses else _Response(200)


@pytest.fixture
def sent(monkeypatch):
    """Capture what reaches the wire, and what gets written to the log."""
    logged: list = []
    monkeypatch.setattr(alerts.db_module, "get_telegram_config",
                        lambda: {"enabled": 1, "bot_token_enc": "t", "chat_id": "c"})
    monkeypatch.setattr(alerts.db_module, "log_telegram_event",
                        lambda et, tid, status, detail: logged.append((et, status)))
    monkeypatch.setattr(alerts, "_is_debug", lambda: False)
    return logged


def _install(monkeypatch, client):
    monkeypatch.setattr(alerts.httpx, "AsyncClient", lambda **kw: client)


class TestAParseFailureIsRecovered:
    @pytest.mark.asyncio
    async def test_it_retries_without_markdown(self, monkeypatch, sent):
        client = _Client(_Response(400, PARSE_ERROR), _Response(200))
        _install(monkeypatch, client)

        ok = await alerts.send_message("*Bad* _markup", "t-1", "ea_bridge_lost")

        assert ok is True
        assert len(client.posts) == 2
        assert "parse_mode" not in client.posts[1]

    @pytest.mark.asyncio
    async def test_the_text_still_arrives(self, monkeypatch, sent):
        """The point. The operator gets the words, markup or no markup."""
        client = _Client(_Response(400, PARSE_ERROR), _Response(200))
        _install(monkeypatch, client)

        await alerts.send_message("EA Bridge Lost: scalp_runner", "t-1", "ea_bridge_lost")

        assert "scalp_runner" in client.posts[1]["text"]

    @pytest.mark.asyncio
    async def test_the_first_attempt_did_use_markdown(self, monkeypatch, sent):
        """Negative control: a transport that never sets parse_mode would pass
        every test above while quietly dropping all formatting."""
        client = _Client(_Response(400, PARSE_ERROR), _Response(200))
        _install(monkeypatch, client)

        await alerts.send_message("*Bold*", "t-1", "x")

        assert client.posts[0]["parse_mode"] == "Markdown"

    @pytest.mark.asyncio
    async def test_it_says_which_alert_needed_rescuing(self, monkeypatch, sent, caplog):
        """The fallback must not paper over the missing escape. A warning
        naming the event type is how the offending formatter gets found."""
        import logging

        client = _Client(_Response(400, PARSE_ERROR), _Response(200))
        _install(monkeypatch, client)

        with caplog.at_level(logging.WARNING):
            await alerts.send_message("*Bad* _markup", "t-1", "ea_bridge_lost")

        assert "ea_bridge_lost" in caplog.text


class TestOtherFailuresAreNotRetried:
    @pytest.mark.asyncio
    async def test_a_different_400_is_not_retried(self, monkeypatch, sent):
        """A bad chat id is not fixed by dropping the markup, and retrying
        every 400 would double every genuine failure."""
        client = _Client(_Response(400, json.dumps(
            {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})))
        _install(monkeypatch, client)

        ok = await alerts.send_message("hello", "t-1", "x")

        assert ok is False
        assert len(client.posts) == 1

    @pytest.mark.asyncio
    async def test_a_server_error_is_not_retried(self, monkeypatch, sent):
        client = _Client(_Response(500, "upstream exploded"))
        _install(monkeypatch, client)

        ok = await alerts.send_message("hello", "t-1", "x")

        assert ok is False
        assert len(client.posts) == 1

    @pytest.mark.asyncio
    async def test_a_success_is_not_retried(self, monkeypatch, sent):
        client = _Client(_Response(200))
        _install(monkeypatch, client)

        await alerts.send_message("hello", "t-1", "x")

        assert len(client.posts) == 1


class TestTheRetryCanAlsoFail:
    @pytest.mark.asyncio
    async def test_it_reports_failure_rather_than_claiming_success(
        self, monkeypatch, sent,
    ):
        client = _Client(_Response(400, PARSE_ERROR), _Response(400, "still no"))
        _install(monkeypatch, client)

        ok = await alerts.send_message("hello", "t-1", "x")

        assert ok is False


class TestTheSiteThatWasFailing:
    def test_the_ea_bridge_lost_alert_escapes_its_strategy(self):
        """The delivery fallback rescues it, but the message then loses ALL
        its formatting. Escaping at the site keeps the bold header working."""
        import pathlib

        from backend.src.services.positions import monitor_loop

        src = pathlib.Path(monitor_loop.__file__).read_text(encoding="utf-8")
        block = src[src.index("*EA Bridge Lost*"):]
        block = block[:block.index('"ea_bridge_lost"')]

        assert "_md_esc" in block or "md_esc" in block

    def test_a_one_underscore_strategy_is_what_broke_it(self):
        """The mechanism, stated as a test so the reason survives. One
        underscore is an unbalanced italic delimiter."""
        assert alerts._md_esc("scalp_runner") == r"scalp\_runner"
        assert "_" in "scalp_runner" and "scalp_runner".count("_") == 1


class TestARepeatedConditionDoesNotRepeat_The_Alert:
    """45 identical push notifications for one trade, measured.

    During the 2026-09-01 demo session `_report_close_refused` sent
    "Close refused by the broker" once a second for 45 seconds while
    AutoTrading was off — 45 Telegram messages for one condition.

    When the log side of this was throttled an hour earlier the alert was
    deliberately left alone, with the reasoning "a message to the operator is
    not log noise". That reasoning is wrong at this volume: 45 notifications
    for one unchanging fact is not information, it is the operator's phone
    being used against him, and the 46th is no more useful than the 2nd.

    Retrying the close IS right — the target is still met and AutoTrading may
    come back. Saying so every second is not.
    """

    @pytest.mark.asyncio
    async def test_the_same_refusal_alerts_once(self, monkeypatch, sent):
        from backend.src.services.positions import monitor_loop
        from backend.src.utils import log_throttle

        log_throttle.reset()
        client = _Client(*[_Response(200)] * 10)
        _install(monkeypatch, client)
        monkeypatch.setattr(monitor_loop.telegram_alerts, "send_message",
                            alerts.send_message)

        for _ in range(10):
            monitor_loop._report_close_refused(
                {"trade_id": "t-1", "mt5_ticket": 111}, "broker refused: 10027")

        import asyncio
        await asyncio.sleep(0)
        assert len([p for p in client.posts]) <= 1

    @pytest.mark.asyncio
    async def test_a_different_refusal_alerts_again(self, monkeypatch, sent):
        """A new reason is new information. Only the unchanged repeat is
        suppressed."""
        from backend.src.services.positions import monitor_loop
        from backend.src.utils import log_throttle

        log_throttle.reset()
        client = _Client(*[_Response(200)] * 10)
        _install(monkeypatch, client)
        monkeypatch.setattr(monitor_loop.telegram_alerts, "send_message",
                            alerts.send_message)

        monitor_loop._report_close_refused(
            {"trade_id": "t-1", "mt5_ticket": 111}, "broker refused: 10027")
        monitor_loop._report_close_refused(
            {"trade_id": "t-1", "mt5_ticket": 111}, "broker refused: 10018 market closed")

        import asyncio
        await asyncio.sleep(0)
        assert len(client.posts) == 2

    @pytest.mark.asyncio
    async def test_another_trade_still_alerts(self, monkeypatch, sent):
        """Two trades both refusing must both be reported. Suppressing by
        condition rather than by trade would hide the second one entirely."""
        from backend.src.services.positions import monitor_loop
        from backend.src.utils import log_throttle

        log_throttle.reset()
        client = _Client(*[_Response(200)] * 10)
        _install(monkeypatch, client)
        monkeypatch.setattr(monitor_loop.telegram_alerts, "send_message",
                            alerts.send_message)

        monitor_loop._report_close_refused(
            {"trade_id": "t-1", "mt5_ticket": 111}, "broker refused: 10027")
        monitor_loop._report_close_refused(
            {"trade_id": "t-2", "mt5_ticket": 222}, "broker refused: 10027")

        import asyncio
        await asyncio.sleep(0)
        assert len(client.posts) == 2
