"""One scan pass reads each channel's parser config once, not once per
buffered message (bugs/030, after the React port).

The scanner walks the reader's whole buffer (up to 100 messages) about once a
second, on the event loop, and asked the database for the channel's config
for every message. `get_channel_parser_config` was the top of the stack in
27 loop stalls over a second on the Mac.

The memo lives for one pass only, so a channel switched off is seen on the
very next pass, exactly as before. A longer-lived cache was rejected for
that reason (see the bug file).
"""
from __future__ import annotations

from backend.src.db import database as db

from tests.core.test_high_risk_skip_log_spam import (  # noqa: F401
    _engine, _msg, _scan, clean_seen, excluding,
)


def _count(monkeypatch):
    calls = []
    real = db.get_channel_parser_config
    monkeypatch.setattr(db, "get_channel_parser_config",
                        lambda name: calls.append(name) or real(name))
    return calls


def test_one_read_per_channel_per_pass(excluding, make_engine, monkeypatch):
    db.save_channel_parser_config("GOLD DIGGERS INSTITUTIONAL", "auto", "", True, True, "")
    calls = _count(monkeypatch)
    msgs = [_msg(str(i), "good morning traders") for i in range(1, 6)]
    _scan(_engine(make_engine, msgs))
    assert calls == ["GOLD DIGGERS INSTITUTIONAL"]


def test_the_next_pass_reads_it_again(excluding, make_engine, monkeypatch):
    db.save_channel_parser_config("GOLD DIGGERS INSTITUTIONAL", "auto", "", True, True, "")
    calls = _count(monkeypatch)
    eng = _engine(make_engine, [_msg("1", "good morning traders")])
    _scan(eng)
    _scan(eng)
    assert len(calls) == 2


def test_a_channel_switched_off_between_passes_is_skipped_next_pass(
        excluding, make_engine, monkeypatch):
    db.save_channel_parser_config("GOLD DIGGERS INSTITUTIONAL", "auto", "", True, True, "")
    eng = _engine(make_engine, [_msg("1", "good morning traders")])
    _scan(eng)
    db.save_channel_parser_config("GOLD DIGGERS INSTITUTIONAL", "none", "", True, True, "")
    calls_after = []
    from backend.src.services.signals import scan_messages as sm
    real = sm._handle_signal_edit_impl
    monkeypatch.setattr(sm, "_handle_signal_edit_impl",
                        lambda *a, **k: calls_after.append(a) or real(*a, **k))
    _scan(eng)
    assert calls_after == []
