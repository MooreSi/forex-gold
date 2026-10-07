"""A price typed with a stray full stop is read as the price (handover 037,
owner 2026-10-07: option B).

2026-09-14, Gold Diggers Scalping: "4284.5. - 4278.5". The parser took the
trailing full stop as part of the number and could not read it, so the live
scan would have skipped the signal with only a log line. Now one trailing
"." is dropped before reading. A trailing "," already worked (commas are
read as thousands separators). Anything still not a number is refused as
before.
"""
import pytest

from backend.src.services.signals import parser


def test_a_trailing_full_stop_is_dropped():
    assert parser._f("4284.5.") == 4284.5
    assert parser._f("4284.") == 4284.0


def test_a_trailing_comma_still_works():
    assert parser._f("4284.5,") == 4284.5


def test_a_number_that_is_still_garbled_is_refused():
    with pytest.raises(ValueError):
        parser._f("4284.5.6")
    with pytest.raises(ValueError):
        parser._f("4284..5")


TYPO_MSG = ("Buy Gold Now\n\n4284.5. - 4278.5\n\nTP 4287\nTP 4290\nTP 4293\n"
            "TP 4296\nTP open\n\nSL 4274")


def test_the_live_scan_reads_the_september_14_message():
    """This channel's layout ("Buy Gold Now" + plain "TP" lines) reaches the
    live scan through the partial reader -- direction and entry zone, with the
    template supplying stop and targets. That reader raised on "4284.5."; it
    now reads the zone like the channel's other messages."""
    p = parser.parse_gd2_partial(TYPO_MSG)
    assert p is not None and p["direction"] == "BUY"
    assert (p["entry_low"], p["entry_high"]) == (4278.5, 4284.5)
