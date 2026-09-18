"""REF signal backfill: reparse stored telegram_messages into
vantage_tg_signals so a window where the app was down, or accept_tg_signals
was off, doesn't permanently lose the reference channels' entries.

Safety surface: this writes signal rows only. It must never mark anything
executable, and it must never re-record a message that already has a row.
Nothing here touches MT5.
"""
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.src.db import database as db
from backend.src.services.positions.core_ref_signal_backfill import (
    backfill_ref_signals, parse_stored_message,
)


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    db._db_executor.submit(_reset_thread_local_connection).result()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db.save_channel_parser_config("Pro Channel", "gd2", "", False, True, "")
    yield db
    _reset_thread_local_connection()
    db._db_executor.submit(_reset_thread_local_connection).result()
    os.remove(path)


# A real C3 entry, verbatim in shape from GOLD DIGGERS INSTITUTIONAL.
ENTRY_MSG = ("BUY LIMITS GOLD @ 4021/4015 AREA\n\nTP 4024\nTP 4028\nTP 4033\n"
             "TP OPEN\nSL 4014\n\nHIGH RISK TRADE")
CHATTER_MSG = "ABSOLUTE CRAZY SCENES 🔥 AUGUST WILL BE BIG"


def _store_message(text, minutes_ago=30, tg_id="1001", channel="Pro Channel"):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO telegram_messages "
            "(telegram_message_id, group_id, group_name, sender_id, sender_name, "
            " timestamp, received_at, text, has_media) "
            "VALUES (?,?,?,?,?,?,?,?,0)",
            (tg_id, "g1", channel, "s1", "sender", ts, ts, text),
        )
    return ts


def _signals():
    with db.db() as conn:
        return [db.row_to_dict(r) for r in
                conn.execute("SELECT * FROM vantage_tg_signals").fetchall()]


# ── parsing ──────────────────────────────────────────────────────────────

def test_parses_a_real_c3_entry():
    p = parse_stored_message(ENTRY_MSG, "gd2")
    assert p is not None
    assert p["direction"] == "BUY"
    assert p["stop_loss"] == 4014.0
    assert p["tp1"] == 4024.0


def test_c3_layout_is_found_even_on_a_format_ab_channel():
    """classify_and_parse checks the limit-order layout ahead of the
    per-channel branch for exactly this reason; the backfill has to match or
    it would silently miss these on a format_ab channel."""
    assert parse_stored_message(ENTRY_MSG, "format_ab") is not None


def test_chatter_is_not_a_signal():
    assert parse_stored_message(CHATTER_MSG, "gd2") is None
    assert parse_stored_message("", "gd2") is None
    assert parse_stored_message(None, "gd2") is None


def test_non_xauusd_is_refused_on_format_ab():
    msg = "Currency: EURUSD\nDirection: BUY\nENTRY: 1.05 - 1.06\nSL: 1.04\nTP1 1.07"
    assert parse_stored_message(msg, "format_ab") is None


# ── backfill behaviour ───────────────────────────────────────────────────

def test_records_a_missing_signal(fresh_db):
    _store_message(ENTRY_MSG)
    res = backfill_ref_signals(lookback_hours=24)
    assert res["recorded"] == 1
    rows = _signals()
    assert len(rows) == 1
    assert rows[0]["direction"] == "BUY"
    assert rows[0]["entry_low"] == 4015.0


def test_backfilled_rows_are_not_executable(fresh_db):
    """The whole safety property: recorded for correlation and learning, never
    for trading. 'historical' is the status the live path already uses for a
    signal recorded too late to trade."""
    _store_message(ENTRY_MSG)
    backfill_ref_signals(lookback_hours=24)
    assert _signals()[0]["status"] == "historical"


def test_parsed_at_comes_from_the_message_not_now(fresh_db):
    """Correlation compares this against the engine's own signal time to
    decide who fired first, so stamping it with now() would corrupt every
    lead/lag measurement built on it."""
    _store_message(ENTRY_MSG, minutes_ago=90)
    backfill_ref_signals(lookback_hours=24)
    parsed_at = float(_signals()[0]["parsed_at"])
    assert 80 * 60 < time.time() - parsed_at < 100 * 60


def test_chatter_is_not_recorded(fresh_db):
    _store_message(CHATTER_MSG)
    assert backfill_ref_signals(lookback_hours=24)["recorded"] == 0
    assert _signals() == []


def test_is_idempotent(fresh_db):
    _store_message(ENTRY_MSG)
    assert backfill_ref_signals(lookback_hours=24)["recorded"] == 1
    assert backfill_ref_signals(lookback_hours=24)["recorded"] == 0
    assert len(_signals()) == 1


def test_does_not_overwrite_a_live_scanned_signal(fresh_db):
    """A message the live path already recorded must be left exactly as it
    is -- in particular its status, which may be mid-lifecycle ('pending',
    'active'), must not be reset to 'historical'."""
    _store_message(ENTRY_MSG, tg_id="2002")
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals "
            "(tg_message_id, group_id, group_name, raw_text, parsed_at, direction, "
            " entry_low, entry_high, status) VALUES (?,?,?,?,?,?,?,?,?)",
            ("2002", "g1", "Pro Channel", ENTRY_MSG, time.time(), "BUY",
             4015.0, 4021.0, "pending"),
        )
    assert backfill_ref_signals(lookback_hours=24)["recorded"] == 0
    rows = _signals()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


def test_respects_the_lookback_window(fresh_db):
    _store_message(ENTRY_MSG, minutes_ago=60 * 24 * 5)  # 5 days old
    assert backfill_ref_signals(lookback_hours=72)["recorded"] == 0


def test_scans_multiple_messages_and_records_only_the_signals(fresh_db):
    _store_message(ENTRY_MSG, tg_id="1", minutes_ago=50)
    _store_message(CHATTER_MSG, tg_id="2", minutes_ago=40)
    _store_message(ENTRY_MSG.replace("BUY", "SELL").replace("4021/4015", "4060/4066"),
                   tg_id="3", minutes_ago=30)
    res = backfill_ref_signals(lookback_hours=24)
    assert res["scanned"] == 3
    assert res["recorded"] == 2
    assert {r["direction"] for r in _signals()} == {"BUY", "SELL"}


# ── one bad message must not abandon the pass ────────────────────────────

# Verbatim from telegram_messages id=1205040, 'Gold Diggers Scalping',
# 2026-09-14T15:30:17Z. The entry line carries a typed full stop --
# "4284.5." -- and _GD2_ENTRY_RANGE_RE's [\d.,]+ captures it, so parser._f
# raises ValueError on it. Live, that killed every hourly backfill pass from
# 2026-09-14 15:30 onwards: 15 parseable REF signals in the 72h window and
# none of them recorded, because the loop's only guard was around the whole
# of it (docs/todo/bugs/061).
TYPO_MSG = ("Buy Gold Now\n\n4284.5. - 4278.5\n\nTP 4287\nTP 4290\nTP 4293\n"
            "TP 4296\nTP open\n\nSL 4274")


def test_the_typo_message_still_raises_in_the_parser():
    """The containment fix below is worth nothing if this stops raising --
    it would then be testing an empty branch. This pins the input, not the
    parser: changing _f to accept a trailing full stop is a change to what
    the LIVE scan can execute and is the owner's call, not this module's."""
    with pytest.raises(ValueError):
        parse_stored_message(TYPO_MSG, "gd2")


def test_a_message_the_parser_chokes_on_does_not_lose_the_others(fresh_db):
    _store_message(ENTRY_MSG, tg_id="1", minutes_ago=50)
    _store_message(TYPO_MSG, tg_id="2", minutes_ago=40)
    _store_message(ENTRY_MSG.replace("BUY", "SELL").replace("4021/4015", "4060/4066"),
                   tg_id="3", minutes_ago=30)

    res = backfill_ref_signals(lookback_hours=24)

    assert res["recorded"] == 2, "the two good messages either side must survive"
    assert res["unparseable"] == 1
    assert {r["direction"] for r in _signals()} == {"BUY", "SELL"}


def test_a_bad_message_before_every_good_one_still_records_them(fresh_db):
    """Ordering matters: rows come back oldest-first, so a bad message early
    in the window is the worst case -- it is what took the live pass from
    15 recorded to 0."""
    _store_message(TYPO_MSG, tg_id="1", minutes_ago=60)
    _store_message(ENTRY_MSG, tg_id="2", minutes_ago=50)

    res = backfill_ref_signals(lookback_hours=24)

    assert res["recorded"] == 1
    assert res["unparseable"] == 1


# ── one pipeline for every channel, here too ─────────────────────────────

# GD2's own layout: direction line, then a range, then TP/SL lines. Nothing
# in it matches Format A/B.
GD2_MSG = ("XAUUSD BUY NOW\n\n4284.0 - 4278.0\n\nTP1 4287\nTP2 4290\n"
           "SL 4274")
# Format A/B's layout: Direction/ENTRY/SL/TP keywords. Nothing in it is GD2.
FORMAT_AB_MSG = ("Risk disclaimer\nCurrency: XAUUSD\nDirection: SELL\n"
                 "ENTRY: 4340.0 - 4344.0\nSL: 4350.0\nTP1 4335\nTP2 4330")


def test_a_gd2_layout_is_parsed_on_a_format_ab_channel():
    """`classify_and_parse` stopped branching on the channel's configured
    parser_format on 2026-08-27 (owner directive: the same parsing rules for
    every channel) because a well-formed signal in the "wrong" layout for its
    channel was being dropped. This module was written a month earlier, still
    branched, and its docstring claimed to mirror that function
    (docs/todo/bugs/061)."""
    p = parse_stored_message(GD2_MSG, "format_ab")
    assert p is not None and p["direction"] == "BUY"
    assert p["stop_loss"] == 4274.0


def test_a_format_ab_layout_is_parsed_on_a_gd2_channel():
    p = parse_stored_message(FORMAT_AB_MSG, "gd2")
    assert p is not None and p["direction"] == "SELL"
    assert p["stop_loss"] == 4350.0


# Verbatim shape from Gold Diggers VIP, the live format_ab channel, with the
# currency swapped. The pair is the ONLY thing that should stop this being
# recorded -- a message the parser cannot read anyway proves nothing about
# the guard, which is how the first version of this test let a mutant that
# deleted the guard survive.
_EURUSD_MSG = ("This is not financial advice.\n\nDirection BUY\n\n"
               "Currency: EURUSD\nENTRY : 1.0940-1.0930\n"
               "TP1: 1.0960\nTP2: 1.0975\n\nSL:  1.0900")


def test_the_same_message_in_xauusd_is_recorded():
    """Half of the guard's test: without this, `is None` below would pass
    even if the parser simply could not read the layout."""
    p = parse_stored_message(_EURUSD_MSG.replace("EURUSD", "XAUUSD"), "format_ab")
    assert p is not None and p["direction"] == "BUY"


def test_non_xauusd_is_refused_whatever_the_channel_is_configured_as():
    """The currency guard was inside the format_ab branch only, so a
    non-XAUUSD signal on a gd2 channel got past it."""
    for fmt in ("format_ab", "gd2", "auto"):
        assert parse_stored_message(_EURUSD_MSG, fmt) is None, fmt


def test_the_guard_catches_more_than_the_one_error_we_happen_to_have(
        fresh_db, monkeypatch):
    """A mutant narrowing the guard to `except ValueError` survived the two
    tests above, because the one real malformed message we have raises a
    ValueError. The property being defended is not "a ValueError is
    contained" -- it is that NO failure on one message abandons the pass, the
    same wording the live scan's guard carries. A parser fed years of
    arbitrary channel text can raise TypeError, KeyError or AttributeError
    just as easily.
    """
    import backend.src.services.positions.core_ref_signal_backfill as mod

    real = mod.parse_stored_message

    def _explode(text, fmt, prefix):
        if "BOOM" in text:
            raise TypeError("not a ValueError")
        return real(text, fmt, prefix)

    _store_message("BOOM", tg_id="1", minutes_ago=60)
    _store_message(ENTRY_MSG, tg_id="2", minutes_ago=50)
    monkeypatch.setattr(mod, "parse_stored_message", _explode)

    res = backfill_ref_signals(lookback_hours=24)

    assert res["recorded"] == 1
    assert res["unparseable"] == 1
