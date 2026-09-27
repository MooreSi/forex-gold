"""Per-channel daily loss cap: one bad signal provider cannot spend the day.

Once a Telegram channel's realised P&L for today falls to minus its cap, new
automated entries FROM THAT CHANNEL are refused until the day turns over.
Every other channel keeps trading. Open positions are never touched -- this is
an entry gate, not a liquidation, and it never goes near the close path.

It is read inside `check_trading_schedule`, because every automated route to
the broker that carries a channel already calls that with the channel's name
(resolution, IME, scan auto-execute, pending activation, resting
revalidation, both EA event paths). A per-route call is how a route gets
missed. It runs ahead of the schedule's master switch: the cap is its own
setting, and turning the windows off must not turn it off too.

Off by default (cap 0), so an install that never sets it trades exactly as
before.

Nothing here reaches a broker: every test works on a private database and
calls the gate function directly.
"""
from datetime import datetime

import pytest

from backend.src.db import database as db_module
from backend.src.services.risk import channel_loss_cap as cap
from backend.src.services.risk import schedule as sched

_WEDNESDAY = datetime(2026, 7, 22, 10, 30, 0)
_DAY_START = _WEDNESDAY.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

_CH = "GOLD DIGGERS VIP"
_OTHER = "GOLD SNIPERS"


def _book(fresh_db, tg_source, pnl, close_time, ticket=True):
    """A closed trade from `tg_source`, closed at `close_time`."""
    _book.n += 1
    tid = f"t{_book.n}"
    with fresh_db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{tid}", "BUY", 2399.0, 2401.0, 2390.0, "filled", close_time - 600),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades "
            "(trade_id, signal_id, direction, entry_low, entry_high, entry_price, "
            " lot_size, remaining_lots, stop_loss, status, open_time, close_time, "
            " net_pnl, tg_source, mt5_ticket) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, f"sig-{tid}", "BUY", 2399.0, 2401.0, 2400.0, 0.1, 0.1, 2390.0,
             "closed", close_time - 600, close_time, pnl, tg_source,
             1000 + _book.n if ticket else None),
        )


_book.n = 0


def _today(hour):
    return _DAY_START + hour * 3600


def _check(source):
    return sched.check_trading_schedule(now=_WEDNESDAY, source=source)


# ── Off by default ──────────────────────────────────────────────────────────

def test_no_cap_configured_never_blocks(fresh_db):
    _book(fresh_db, _CH, -900.0, _today(9))
    assert _check(_CH) == (True, "")


# ── The gate ────────────────────────────────────────────────────────────────

def test_a_channel_past_its_cap_is_refused_and_the_reason_names_it(fresh_db):
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, _CH, -70.0, _today(8))
    _book(fresh_db, _CH, -50.0, _today(9))

    allowed, reason = _check(_CH)

    assert allowed is False
    assert _CH in reason
    assert "-120.00" in reason
    assert "100.00" in reason


def test_exactly_at_the_cap_is_refused(fresh_db):
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, _CH, -100.0, _today(9))
    assert _check(_CH)[0] is False


def test_short_of_the_cap_is_allowed(fresh_db):
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, _CH, -99.0, _today(9))
    assert _check(_CH) == (True, "")


def test_other_channels_keep_trading(fresh_db):
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, _CH, -300.0, _today(9))
    _book(fresh_db, _OTHER, -20.0, _today(9))
    assert _check(_CH)[0] is False
    assert _check(_OTHER) == (True, "")


def test_it_is_net_so_an_earlier_win_offsets_a_later_loss(fresh_db):
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, _CH, 80.0, _today(8))
    _book(fresh_db, _CH, -150.0, _today(9))   # net -70
    assert _check(_CH) == (True, "")


def test_yesterdays_losses_do_not_count(fresh_db):
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, _CH, -500.0, _DAY_START - 60)
    assert _check(_CH) == (True, "")


def test_counted_by_close_time_so_an_overnight_loser_counts_today(fresh_db):
    """Opened yesterday, closed today at a loss: that money was lost today."""
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, _CH, -150.0, _DAY_START + 60)   # opened 10 min earlier, yesterday
    assert _check(_CH)[0] is False


def test_trades_with_no_broker_ticket_do_not_count(fresh_db):
    """Simulated rows are not money. The sim ledger has produced a false halt
    before (risk README, 2026-07-07); the channel scorecard filters them the
    same way."""
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, _CH, -500.0, _today(9), ticket=False)
    assert _check(_CH) == (True, "")


def test_the_cap_holds_with_the_schedule_switched_off(fresh_db):
    """The windows' master switch is not this gate's switch."""
    sched.set_trading_schedule_enabled(False)
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, _CH, -150.0, _today(9))
    assert _check(_CH)[0] is False


def test_the_cap_holds_with_the_schedule_switched_on(fresh_db):
    """And it is checked before the windows, so it is not skipped by an early
    return from a window that would otherwise allow the trade."""
    from tests.core.test_trading_schedule import _schedule_with_one_block
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_schedule_with_one_block(start="09:00", end="12:00"))
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, _CH, -150.0, _today(9))
    assert _check(_CH)[0] is False


# ── Channel names ───────────────────────────────────────────────────────────

def test_a_wrapped_source_name_is_the_same_channel(fresh_db):
    """Stored signals carry "Telegram Auto (<channel>)"; the routes pass either
    form. Both the trades and the gate must resolve to the one channel, or a
    loss booked under one spelling never stops entries under the other."""
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, f"Telegram Auto ({_CH})", -80.0, _today(8))
    _book(fresh_db, _CH, -40.0, _today(9))
    assert _check(_CH)[0] is False
    assert _check(f"Telegram Auto ({_CH})")[0] is False


def test_the_engines_are_not_channels(fresh_db):
    """The Reversal and Breakout engines have their own loss controls, and the
    request was per Telegram channel."""
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, "Reversal Engine", -500.0, _today(9))
    _book(fresh_db, "Breakout Engine", -500.0, _today(9))
    assert _check("reversal_engine") == (True, "")
    assert _check("breakout_engine") == (True, "")
    assert _check("Reversal Engine") == (True, "")


def test_a_caller_that_names_no_channel_is_not_gated(fresh_db):
    cap.set_caps(default_cap=100.0, overrides={})
    _book(fresh_db, _CH, -500.0, _today(9))
    assert sched.check_trading_schedule(now=_WEDNESDAY) == (True, "")


# ── Per-channel overrides ───────────────────────────────────────────────────

def test_a_channel_override_replaces_the_default(fresh_db):
    cap.set_caps(default_cap=100.0, overrides={_CH: 300.0})
    _book(fresh_db, _CH, -150.0, _today(9))
    _book(fresh_db, _OTHER, -150.0, _today(9))
    assert _check(_CH) == (True, "")
    assert _check(_OTHER)[0] is False


def test_an_override_of_zero_exempts_that_channel(fresh_db):
    cap.set_caps(default_cap=100.0, overrides={_CH: 0.0})
    _book(fresh_db, _CH, -500.0, _today(9))
    assert _check(_CH) == (True, "")


def test_an_override_can_cap_one_channel_with_no_default(fresh_db):
    cap.set_caps(default_cap=0.0, overrides={_CH: 50.0})
    _book(fresh_db, _CH, -60.0, _today(9))
    _book(fresh_db, _OTHER, -600.0, _today(9))
    assert _check(_CH)[0] is False
    assert _check(_OTHER) == (True, "")


def test_an_override_keyed_by_a_wrapped_name_is_stored_under_the_channel(fresh_db):
    cap.set_caps(default_cap=0.0, overrides={f"Telegram Auto ({_CH})": 50.0})
    assert cap.get_overrides() == {_CH: 50.0}


# ── Validation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("default_cap, overrides", [
    (-1.0, {}),
    (0.0, {_CH: -5.0}),
    (float("nan"), {}),
    (0.0, {_CH: float("inf")}),
])
def test_a_negative_or_non_finite_cap_is_refused_and_nothing_is_stored(
        fresh_db, default_cap, overrides):
    cap.set_caps(default_cap=100.0, overrides={_OTHER: 20.0})
    with pytest.raises(ValueError):
        cap.set_caps(default_cap=default_cap, overrides=overrides)
    assert cap.get_default_cap() == 100.0
    assert cap.get_overrides() == {_OTHER: 20.0}


def test_an_unreadable_stored_value_reads_as_off(fresh_db):
    db_module.set_app_config(cap.DEFAULT_KEY, "not a number")
    db_module.set_app_config(cap.OVERRIDES_KEY, "{broken json")
    assert cap.get_default_cap() == 0.0
    assert cap.get_overrides() == {}


# ── Failure direction ───────────────────────────────────────────────────────

def test_an_armed_cap_that_cannot_read_todays_pnl_refuses(fresh_db, monkeypatch):
    """The risk domain fails toward refuse-to-trade. A cap that cannot see the
    day's losses has not been told they are fine."""
    cap.set_caps(default_cap=100.0, overrides={})

    def _boom(_since):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cap.risk_repo, "closed_pnl_by_source_since", _boom)
    allowed, reason = _check(_CH)
    assert allowed is False
    assert "could not" in reason.lower()


def test_an_unarmed_cap_never_reads_the_trades(fresh_db, monkeypatch):
    """Off must mean off, including when the trade table is unreadable."""
    def _boom(_since):
        raise AssertionError("read the trades with no cap set")

    monkeypatch.setattr(cap.risk_repo, "closed_pnl_by_source_since", _boom)
    assert _check(_CH) == (True, "")


# ── The screen's read ───────────────────────────────────────────────────────

def test_state_reports_each_channel_with_its_cap_pnl_and_whether_it_is_held(
        fresh_db, monkeypatch):
    monkeypatch.setattr(cap, "_channel_names", lambda: [_CH, _OTHER])
    cap.set_caps(default_cap=100.0, overrides={_OTHER: 0.0})
    _book(fresh_db, _CH, -120.0, _today(9))
    _book(fresh_db, _OTHER, -40.0, _today(9))

    state = cap.state(now=_WEDNESDAY)

    assert state["default_cap"] == 100.0
    assert state["overrides"] == {_OTHER: 0.0}
    rows = {r["channel"]: r for r in state["channels"]}
    assert rows[_CH] == {"channel": _CH, "cap": 100.0, "day_pnl": -120.0, "held": True}
    assert rows[_OTHER] == {"channel": _OTHER, "cap": 0.0, "day_pnl": -40.0, "held": False}


# ── Travels with the schedule to the paired node ────────────────────────────

def test_the_caps_ride_the_schedule_snapshot(fresh_db):
    cap.set_caps(default_cap=75.0, overrides={_CH: 40.0})
    snap = sched.trading_schedule_snapshot()
    assert snap["channel_loss_cap"] == 75.0
    assert snap["channel_loss_caps"] == {_CH: 40.0}


def test_applying_a_snapshot_sets_the_caps(fresh_db):
    sched.apply_trading_schedule_snapshot(
        {"channel_loss_cap": 60.0, "channel_loss_caps": {_OTHER: 10.0}})
    assert cap.get_default_cap() == 60.0
    assert cap.get_overrides() == {_OTHER: 10.0}


def test_a_snapshot_from_an_older_peer_leaves_the_caps_alone(fresh_db):
    """Absent is how a peer that predates the field speaks. Reading it as
    "clear" would switch the cap off on every sync tick."""
    cap.set_caps(default_cap=75.0, overrides={_CH: 40.0})
    sched.apply_trading_schedule_snapshot({"enabled": False})
    assert cap.get_default_cap() == 75.0
    assert cap.get_overrides() == {_CH: 40.0}


def test_the_schedule_screen_gets_none_not_a_cheerful_default(fresh_db, monkeypatch):
    """A card saying "no caps" when the caps could not be read would say
    entries are allowed that may be held."""
    def _boom():
        raise RuntimeError("channel table gone")

    monkeypatch.setattr(cap, "_channel_names", _boom)
    assert cap._screen_state() is None
