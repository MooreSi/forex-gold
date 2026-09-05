"""A bare-direction message must not be re-PARSED on every scan cycle either.

bugs/015, the half left open when the logging was silenced on 2026-08-28. The
message still reached every parser in classify_and_parse about once a second
for as long as it stayed in the reader's fetch window -- the Format A/B parser,
the GD2 parser, both partial parsers and both instant-entry parsers, against
the same unchanged text, forever.

WHY THIS DOES NOT RECORD THE MESSAGE, which is what 015 proposed. Two things
were checked in the code before building this and both make that fix wrong as
written:

1. It would not help the follow-up. 015 says a parked `vantage_tg_signals` row
   "gives the later full-levels message something to complete". The follow-up
   matcher does not look at that table. `attach_followup_levels` selects from
   `vantage_second_message_holds WHERE status='waiting'` and nothing else, so a
   parked signals row is invisible to it.

2. It would lose a trade. `scan_messages.py`'s dedup probe routes ANY message
   that already has a row into `_handle_signal_edit_impl`. There, an edit that
   adds full levels to a row whose status is not `pending_followup` updates the
   fields, leaves `_promote_execute` False and returns None -- so the caller
   moves on and never executes it. Today, with no row, that same edit is parsed
   fresh and taken. Parking the message would silently convert a taken trade
   into a missed one.

So the message stays unrecorded and the skip is remembered in-process only,
which changes no signal-parsing behaviour and cannot affect the money path.

KEYED ON THE TEXT, not the id alone. A Telegram edit keeps the message id and
changes the body; that is how a bare direction most often becomes a real
signal. Remembering the id alone would skip the edited text forever and lose
exactly the trade this is trying not to touch.

The guard sits AFTER the learned-rules parser and the second-message block and
before everything else, so both keep their chance on every cycle -- learned
rules because the operator can add one at any time, and the second-message
block because a follow-up is a different message that must still be consumed.
"""
from __future__ import annotations

import pytest

from backend.src.services.signals import scan_parse_classify as spc


pytestmark = pytest.mark.asyncio

BARE = "XAU USD SELL"


async def _classify(tg_id: str, text: str = BARE, **kw):
    async def _no_ai(t, ch, tid):
        return None

    kwargs = dict(
        tg_id=tg_id, group_id="g1", channel_name="Gold Diggers VIP",
        text=text, msg={"timestamp": "", "sender_name": "x"},
        parser_fmt="auto", sig_prefix="",
        ai_fallback_fn=_no_ai, queue_unrecognised_fn=lambda *a: None,
        rs={},
    )
    kwargs.update(kw)
    return await spc.classify_and_parse(**kwargs)


@pytest.fixture(autouse=True)
def clean_seen(fresh_db):
    """A real (empty) database as well as clean module state.

    `parse_lexicon_direction_trigger` reads the Logic Keywords lexicons from
    `logic_keyword_lexicons` behind a short-lived cache, so a run long enough
    for that cache to expire -- which the 50-cycle rescan test is -- reaches
    the database for real. Without one it raises OperationalError from inside
    the parser and the test fails for a reason that has nothing to do with
    what it is asserting.
    """
    spc.reset_bare_direction_log_memory()
    yield
    spc.reset_bare_direction_log_memory()


@pytest.fixture
def parser_calls(monkeypatch):
    """Counts the deterministic parsers the rescan is supposed to skip.

    Wrapping the real ones rather than replacing them: a stub returning None
    would make every message look unparseable and the test would pass for the
    wrong reason.
    """
    calls: list[str] = []

    for name in ("is_format_ab_signal", "is_gd2_message", "parse_gd2_signal",
                 "parse_gold_signal", "parse_gd2_partial",
                 "parse_limit_order_signal"):
        real = getattr(spc, name)

        def _counted(*a, _real=real, _name=name, **k):
            calls.append(_name)
            return _real(*a, **k)
        monkeypatch.setattr(spc, name, _counted)
    return calls


class TestTheRescanDoesNoWork:
    async def test_the_first_scan_runs_the_parsers(self, parser_calls):
        """Negative control. If this were empty the test below would pass
        with the parsers never running at all."""
        await _classify("19886")

        assert parser_calls, "the first sighting must actually be parsed"

    async def test_A_RESCAN_RUNS_NONE_OF_THEM(self, parser_calls):
        """The bug: the same unchanged text, measured against every parser,
        about once a second, indefinitely."""
        await _classify("19886")
        parser_calls.clear()

        for _ in range(50):
            await _classify("19886")

        assert parser_calls == []

    async def test_a_different_message_is_still_parsed(self, parser_calls):
        await _classify("19886")
        parser_calls.clear()

        await _classify("19887")

        assert parser_calls


class TestAnEditIsNotSkipped:
    async def test_the_SAME_ID_WITH_NEW_TEXT_IS_PARSED_AGAIN(self, parser_calls):
        """A Telegram edit keeps the id. This is how a bare direction turns
        into a real signal, so skipping on the id alone would lose it."""
        await _classify("19886", BARE)
        parser_calls.clear()

        await _classify("19886", "XAU USD SELL 3400-3405 SL 3410 TP1 3390")

        assert parser_calls, "an edited message was skipped"

    async def test_an_edit_that_completes_the_signal_is_returned(self):
        """Not merely re-parsed -- the parsed signal must reach the caller,
        because that is what gets executed."""
        await _classify("19886", BARE)

        result = await _classify(
            "19886", "XAU USD SELL 3400-3405 SL 3410 TP1 3390 TP2 3380",
        )

        assert result is not None, "the completed signal never came back"
        assert result["direction"].upper() == "SELL"

    async def test_reverting_to_the_bare_text_is_skipped_again(self, parser_calls):
        """The memory is keyed on the text, so the original body is still
        known after an edit and back."""
        await _classify("19886", BARE)
        await _classify("19886", "some other text entirely")
        parser_calls.clear()

        await _classify("19886", BARE)

        assert parser_calls == []


class TestWhatMustStILLRunOnEveryCycle:
    async def test_learned_rules_still_get_their_chance(self, monkeypatch):
        """The guard sits after them. An operator can add a learned rule at
        any time, and it must apply to a message already parked."""
        await _classify("19886")

        learned = {"direction": "SELL", "entry_low": 3400.0, "entry_high": 3405.0}
        monkeypatch.setattr(
            spc, "parse_with_learned_rules", lambda text, ch: dict(learned),
        )
        result = await _classify("19886")

        assert result == learned

    async def test_the_second_message_block_still_runs(self, monkeypatch):
        """A levels-only follow-up is a DIFFERENT message that must still be
        consumed, and it is handled above the guard.

        The message is parked as a bare direction FIRST, then the same body is
        made consumable as a follow-up. Without that the guard never fires for
        this message and the test passes whatever order the two are in -- which
        is exactly what happened: the first version of this test did not notice
        the guard being moved above the second-message block at all.

        Today's parsers cannot produce that overlap, since a bare direction has
        no SL or TP for parse_tp_sl_only to find. It is asserted anyway because
        the ordering is a deliberate choice and both halves are reachable
        configuration -- second_message_enabled is a setting the operator can
        turn on at any time, and the lexicons that decide what counts as a bare
        direction are user-editable text.
        """
        await _classify("19886", BARE)

        seen: list = []
        monkeypatch.setattr(spc, "second_message_enabled", lambda rs: True)
        monkeypatch.setattr(spc, "parse_tp_sl_only", lambda t: {"stop_loss": 3410.0})
        monkeypatch.setattr(
            spc, "attach_followup", lambda ch, lv: seen.append((ch, lv)) or "19886",
        )

        await _classify("19886", BARE)

        assert seen, "the guard consumed a message the follow-up path owns"


class TestBehaviourIsOtherwiseUnchanged:
    async def test_a_rescan_still_returns_none(self):
        await _classify("19886")

        for _ in range(5):
            assert await _classify("19886") is None

    async def test_a_rescan_is_still_not_queued_as_unrecognised(self):
        queued: list = []
        await _classify("19886")

        for _ in range(5):
            await _classify("19886", queue_unrecognised_fn=lambda *a: queued.append(a))

        assert queued == []
