"""The panel's Pause Trading screen.

Pausing until a session boundary is the point: "stop until London closes" is
a decision about the market, and working out that it is 4h17m away is exactly
the arithmetic you don't want to be doing on a phone to make it.

The boundaries must stay the ones the rest of the app uses
(dpm_engine.detect_session's partition), and the pause must write the same
trade_pause_until key that core_open_trade's gate reads -- a Pause button that
sets a flag nothing enforces is worse than no button, because it is believed.
"""
import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from backend.src.services.positions import core_bot_panel as panel
from backend.src.db import database as db


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _pause_until() -> float:
    return float(db.get_app_config("trade_pause_until") or 0)


def _labels(screen):
    return [b["text"] for row in screen.keyboard for b in row]


# ── session boundaries ───────────────────────────────────────────────────────

def test_boundaries_match_the_apps_own_session_partition():
    """These hours are not a second opinion about when London ends -- they are
    dpm_engine.detect_session's own edges, which is_session_allowed() and the
    analytics heat map also read. If they drifted, "End of London" on the
    panel would mean something different from London everywhere else."""
    ends = {k: h for k, (_, h) in panel._SESSION_END_UTC.items()}
    assert ends == {"as": 7, "lo": 12, "ov": 16, "ny": 21}

    import backend.src.services.dpm.engine as dpm
    # The real partition, not the pinned lambda the fixed_clock plugin installs
    # for the rest of the suite -- this test exists to check the boundaries
    # themselves, so asserting against the pinned stub would prove nothing.
    detect_session = getattr(dpm, "detect_session_unpinned", dpm.detect_session)

    # The hour immediately before each boundary must still be that session,
    # and the boundary hour itself must not be.
    class _FakeDT:
        target = None
        @classmethod
        def now(cls, tz=None):
            return cls.target

    for key, (_label, hour) in panel._SESSION_END_UTC.items():
        for probe, expect_in in ((hour - 1, True), (hour, False)):
            _FakeDT.target = datetime(2026, 8, 18, probe % 24, 30, tzinfo=timezone.utc)
            orig = dpm.datetime
            dpm.datetime = _FakeDT
            try:
                session = detect_session()
            finally:
                dpm.datetime = orig
            name = {"as": "asian", "lo": "london", "ov": "overlap", "ny": "ny"}[key]
            assert (session == name) is expect_in, (
                f"{name}: hour {probe % 24} reported {session}"
            )


def test_next_utc_hour_is_always_in_the_future():
    """At exactly 12:00:00 UTC, 'end of London' means tomorrow's -- not a
    zero-length pause that lifts on the same tick it was set."""
    now = datetime(2026, 8, 18, 12, 0, 0, tzinfo=timezone.utc)
    ts = panel._next_utc_hour(12, now=now)
    assert ts > now.timestamp()
    assert datetime.fromtimestamp(ts, timezone.utc) == now + timedelta(days=1)


def test_next_utc_hour_picks_today_when_the_boundary_is_still_ahead():
    now = datetime(2026, 8, 18, 9, 15, tzinfo=timezone.utc)
    ts = panel._next_utc_hour(12, now=now)
    assert datetime.fromtimestamp(ts, timezone.utc) == datetime(
        2026, 8, 18, 12, 0, tzinfo=timezone.utc)


# ── the screen ───────────────────────────────────────────────────────────────

def test_root_menu_offers_pause_trading(fresh_db):
    assert any("Pause Trading" in t for t in _labels(panel.root_screen()))


def test_pause_screen_offers_every_session_and_custom(fresh_db):
    labels = _labels(panel.pause_trading_screen())
    for name in ("End of NY", "End of Overlap", "End of London", "End of Asian"):
        assert any(name in t for t in labels), f"missing {name}"
    assert any("Custom" in t for t in labels)


def test_session_buttons_keep_a_fixed_order(fresh_db):
    """Ordering by which boundary is soonest would reshuffle the keyboard
    through the day, and a keyboard whose buttons move is one you mis-tap."""
    labels = [t for t in _labels(panel.pause_trading_screen()) if t.startswith("⏸ End of")]
    assert labels == sorted(labels, key=lambda t: ["NY", "Overlap", "London", "Asian"]
                            .index(t.split("End of ")[1].split(" ")[0]))


def test_resume_button_appears_only_while_paused(fresh_db):
    assert not any("Resume" in t for t in _labels(panel.pause_trading_screen()))
    db.set_app_config("trade_pause_until", str(time.time() + 3600))
    assert any("Resume" in t for t in _labels(panel.pause_trading_screen()))


def test_root_button_reports_the_paused_state(fresh_db):
    """A panel that still reads "Pause Trading" while trading is already
    paused is how you end up believing you are flat when you are not."""
    assert "PAUSED" not in " ".join(_labels(panel.root_screen()))
    db.set_app_config("trade_pause_until", str(time.time() + 3600))
    assert any("PAUSED until" in t for t in _labels(panel.root_screen()))


def test_an_expired_pause_reads_as_active(fresh_db):
    db.set_app_config("trade_pause_until", str(time.time() - 60))
    assert panel._pause_state()[0] is False
    assert "PAUSED" not in " ".join(_labels(panel.root_screen()))


# ── acting on it ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,hour", [("as", 7), ("lo", 12), ("ov", 16), ("ny", 21)])
def test_tapping_a_session_pauses_until_that_boundary(fresh_db, key, hour):
    asyncio.run(panel.handle_callback(f"p|ptu|{key}", None))
    until = _pause_until()
    assert until > time.time()
    assert datetime.fromtimestamp(until, timezone.utc).hour == hour
    assert datetime.fromtimestamp(until, timezone.utc).minute == 0


def test_the_pause_uses_the_key_the_trade_gate_actually_reads(fresh_db):
    """core_open_trade blocks order placement on
    core_risk_governor.is_trading_paused(), which reads trade_pause_until. A
    Pause button writing anywhere else would be believed and do nothing."""
    from backend.src.services.risk.governor import is_trading_paused
    assert is_trading_paused() is False
    asyncio.run(panel.handle_callback("p|ptu|ny", None))
    assert is_trading_paused() is True


def test_resume_lifts_the_pause(fresh_db):
    from backend.src.services.risk.governor import is_trading_paused
    asyncio.run(panel.handle_callback("p|ptu|ny", None))
    asyncio.run(panel.handle_callback("p|ptr", None))
    assert is_trading_paused() is False


def test_custom_hours_pauses_for_that_long(fresh_db):
    asyncio.run(panel.handle_value_reply(panel.pause_prompt_text(), "2.5"))
    assert _pause_until() == pytest.approx(time.time() + 2.5 * 3600, abs=5)


def test_custom_hours_accepts_an_h_suffix(fresh_db):
    asyncio.run(panel.handle_value_reply(panel.pause_prompt_text(), "3h"))
    assert _pause_until() == pytest.approx(time.time() + 3 * 3600, abs=5)


@pytest.mark.parametrize("bad", ["abc", "", "0", "-3", "500", "1e9"])
def test_custom_hours_rejects_nonsense_without_pausing(fresh_db, bad):
    """A typo'd 500 that parks trading until next month is not a pause anyone
    meant to set, and neither is a silent no-op that leaves it running."""
    screen = asyncio.run(panel.handle_value_reply(panel.pause_prompt_text(), bad))
    assert _pause_until() == 0
    assert screen.mode == "send" and screen.text


def test_custom_prompt_round_trips_through_the_reply_parser(fresh_db):
    """force_reply carries its target in the prompt text; if the token did not
    parse back, the typed hours would be silently dropped."""
    assert panel.parse_prompt(panel.pause_prompt_text()) == ("hrs", "pause")


# ── Telegram mechanics ───────────────────────────────────────────────────────

def test_every_pause_button_is_within_telegrams_callback_limit(fresh_db):
    """Telegram silently drops the ENTIRE keyboard if any callback_data
    exceeds 64 bytes -- the panel would just stop responding."""
    screens = [panel.root_screen(), panel.pause_trading_screen()]
    db.set_app_config("trade_pause_until", str(time.time() + 3600))
    screens.append(panel.pause_trading_screen())
    for s in screens:
        for row in s.keyboard:
            for b in row:
                assert len(b["callback_data"].encode()) <= 64, b


def test_pause_actions_do_not_collide_with_existing_ones(fresh_db):
    """'pause' was already taken by the per-channel toggle; these are the
    account-wide controls and must route somewhere else entirely."""
    chan_toggle = asyncio.run(panel.handle_callback("p|pause|deadbeef", None))
    assert chan_toggle.mode == "noop"       # unknown channel, not a global pause
    assert _pause_until() == 0
