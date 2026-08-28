"""A bare-direction message must not re-log on every scan cycle.

bugs/015. A message that is a direction trigger with no entry, SL or TP is the
one terminal branch of classify_and_parse that records NOTHING -- every sibling
either inserts a row, queues the message, or records it. Nothing marks it as
seen, so the scan loop handles it again about once a second for as long as it
stays in the reader's fetch window.

Observed live: one 15-character SELL from Gold Diggers VIP, tg_id 19886,
produced 8,319 identical log lines between 16:41 and 19:06 on 2026-08-28 and
was still going. It does not stop on its own.

This covers the LOG half only. The message is still not recorded, so it is
still re-parsed every cycle -- that part changes signal-parsing behaviour and
is left for the owner (see 015). What is fixed here is that the operator sees
it once instead of thousands of times, which is what the line is for.
"""
from __future__ import annotations

import logging

import pytest

from backend.src.services.signals import scan_parse_classify as spc


pytestmark = pytest.mark.asyncio


async def _classify(tg_id: str, text: str = "XAU USD SELL"):
    async def _no_ai(t, ch, tid):
        return None

    return await spc.classify_and_parse(
        tg_id=tg_id, group_id="g1", channel_name="Gold Diggers VIP",
        text=text, msg={"timestamp": "", "sender_name": "x"},
        parser_fmt="auto", sig_prefix="",
        ai_fallback_fn=_no_ai, queue_unrecognised_fn=lambda *a: None,
        rs={},
    )


def _bare_lines(caplog):
    return [r for r in caplog.records if "Bare direction" in r.getMessage()]


@pytest.fixture(autouse=True)
def clean_seen():
    """The suppression is module state, so it must not leak between tests."""
    spc.reset_bare_direction_log_memory()
    yield
    spc.reset_bare_direction_log_memory()


class TestItLogsOnce:
    async def test_the_first_sighting_is_logged(self, caplog):
        with caplog.at_level(logging.INFO, logger=spc.log.name):
            assert await _classify("19886") is None

        assert len(_bare_lines(caplog)) == 1

    async def test_RESCANNING_THE_SAME_MESSAGE_IS_SILENT(self, caplog):
        """The bug. 8,319 lines from one message, and still climbing when it
        was found."""
        with caplog.at_level(logging.INFO, logger=spc.log.name):
            for _ in range(50):
                await _classify("19886")

        assert len(_bare_lines(caplog)) == 1

    async def test_A_DIFFERENT_MESSAGE_IS_STILL_LOGGED(self, caplog):
        """Suppression must be per message. Silencing the second one would
        hide a real bare signal arriving after the first."""
        with caplog.at_level(logging.INFO, logger=spc.log.name):
            await _classify("19886")
            await _classify("19887")

        logged = _bare_lines(caplog)
        assert len(logged) == 2

    async def test_the_line_still_names_the_message_and_direction(self, caplog):
        """It is the only trace this message left. Losing the id would make
        it untraceable."""
        with caplog.at_level(logging.INFO, logger=spc.log.name):
            await _classify("19886", "XAU USD SELL")

        line = _bare_lines(caplog)[0].getMessage()
        assert "19886" in line
        assert "SELL" in line

    async def test_the_message_is_still_HANDLED_the_same_way(self, caplog):
        """Behaviour is unchanged: still skipped, still returns None, still
        not queued as unrecognised. Only the logging is different."""
        queued = []

        async def _no_ai(t, ch, tid):
            return None

        for _ in range(3):
            result = await spc.classify_and_parse(
                tg_id="19886", group_id="g1", channel_name="Gold Diggers VIP",
                text="XAU USD SELL", msg={"timestamp": "", "sender_name": "x"},
                parser_fmt="auto", sig_prefix="",
                ai_fallback_fn=_no_ai,
                queue_unrecognised_fn=lambda *a: queued.append(a),
                rs={},
            )
            assert result is None

        assert queued == [], "a bare direction was queued as unrecognised"


class TestTheMemoryIsBounded:
    async def test_it_does_not_grow_without_limit(self, caplog):
        """It is module state in a process that runs for weeks. An unbounded
        set of every bare message ever seen is a slow leak."""
        with caplog.at_level(logging.INFO, logger=spc.log.name):
            for i in range(spc._BARE_LOG_MEMORY + 200):
                await _classify(str(i))

        assert len(spc._bare_direction_logged) <= spc._BARE_LOG_MEMORY

    async def test_the_MOST_RECENT_message_is_still_remembered(self, caplog):
        """Eviction must drop the oldest. Dropping the newest would restore
        the every-cycle spam for the message currently in the window --
        exactly the case this exists for."""
        for i in range(spc._BARE_LOG_MEMORY + 200):
            await _classify(str(i))

        newest = str(spc._BARE_LOG_MEMORY + 199)
        with caplog.at_level(logging.INFO, logger=spc.log.name):
            await _classify(newest)

        assert _bare_lines(caplog) == []
