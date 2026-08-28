"""The IME follow-up matcher must only claim a trade that is genuinely still
waiting for its follow-up.

Live miss, 2026-08-28 (Gold Diggers VIP, demo). A bare "BUY" trigger opened
trade eec8004d at 11:41:06 and its real follow-up (tg_id 19830, zone
4598-4602) arrived 37s later and was applied correctly. At 12:09:26 a
completely separate BUY signal (tg_id 19832, zone 4592-4596, SL 4590) arrived
while that first trade was still open. find_latest_instant_trade() bounded its
search by nothing but status='open', tg_source and direction, so the 28-minute-
old trade matched again, the new signal was swallowed as its "follow-up", and
scan_auto_execute returned before it could ever open or queue anything. The
signal produced nothing at all -- the trade was managed_by='ea', so even the
levels were discarded.

`tp1 IS NULL` alone does not fix this: ime_timeout_watchdog() skips
EA-managed trades (see its managed_by=='ea' guard), so an EA trade keeps
tp1 NULL for its whole life and would stay eligible forever. The bound that
actually holds is age, against the same ime_followup_timeout_s that defines
when the watchdog gives up waiting for the follow-up.

Fakes only. NO real or demo MT5 order is placed, closed or modified here --
the bridge is tests._fakes._FakeBridge and its call log is asserted empty on
the paths that must not match.
"""
import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.services.risk import expert_params
from backend.src.services.trading import instant_followup as followup
from backend.src.services.trading import trade_repo
from tests._fakes import _FakeBridge


_PARSED = {"stop_loss": 4586.0, "entry_low": 4592.0, "entry_high": 4596.0,
           "tp1": 4598.0, "tp2": 4599.0, "tp3": 4600.0}

_CHAN = "Gold Diggers VIP"


def _insert_trade(trade_id, *, age_s, tp1=None, managed_by="python",
                  strategy="scale_out", direction="BUY", stop_loss=4591.56,
                  entry_price=4603.56, tg_source=_CHAN):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, entry_price, entry_price, stop_loss,
             "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, tp1, "
            "status, open_time, strategy, tg_source, managed_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", 1880277031, direction, entry_price, entry_price,
             entry_price, 0.10, 0.10, stop_loss, tp1, "open", time.time() - age_s,
             strategy, tg_source, managed_by),
        )


def _insert_tg(tg_id):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals (tg_message_id,group_id,group_name,sender_name,"
            "message_ts,raw_text,parsed_at,direction,status) VALUES (?,?,?,?,?,?,?,?,?)",
            (tg_id, "grp", _CHAN, "sender", "", "text", time.time(), "BUY", "new"),
        )


def _trade(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?",
                         (trade_id,)).fetchone()
        )


def _timeout() -> int:
    return expert_params.get("ime_followup_timeout_s")


# ── the live miss ────────────────────────────────────────────────────────────

def test_stale_ea_trade_does_not_absorb_a_later_independent_signal(fresh_db):
    """tg_id 19832: a 28-minute-old, already-laddered, EA-managed BUY must not
    claim a brand-new BUY signal as its follow-up."""
    _insert_trade("eec8004d-adbe-4a", age_s=1700, tp1=4605.56,
                  managed_by="ea", strategy="template:GD VIP - Single")
    _insert_tg("19832")
    bridge = _FakeBridge()

    matched = asyncio.run(followup.find_and_apply_instant_followup(
        _CHAN, "BUY", _PARSED, "19832", bridge,
    ))

    assert matched is False, (
        "a 28-minute-old trade claimed a new signal as its follow-up — "
        "the signal is dropped instead of executing or queueing"
    )
    assert bridge.modify_order_calls == []
    assert _trade("eec8004d-adbe-4a")["stop_loss"] == 4591.56


def test_stale_trade_awaiting_levels_is_also_too_old_to_match(fresh_db):
    """Same bound with tp1 still NULL — age is what disqualifies it, not the
    ladder. This is the EA case ime_timeout_watchdog() never fills in."""
    _insert_trade("t-stale-null", age_s=_timeout() + 60, tp1=None, managed_by="ea")
    _insert_tg("tg-stale")
    bridge = _FakeBridge()

    matched = asyncio.run(followup.find_and_apply_instant_followup(
        _CHAN, "BUY", _PARSED, "tg-stale", bridge,
    ))

    assert matched is False
    assert bridge.modify_order_calls == []


# ── the behaviour that must survive ──────────────────────────────────────────

def test_genuine_followup_37s_after_the_ime_open_still_matches(fresh_db):
    """tg_id 19830: the real follow-up, inside the window. Tightening the
    matcher must not break the feature it exists for. Entry sits below the
    signal's TPs so the apply path takes its normal branch rather than the
    "fewer than 2 reachable TPs" auto-spacing one."""
    _insert_trade("t-fresh", age_s=37, tp1=None, entry_price=4590.0)
    _insert_tg("19830")
    bridge = _FakeBridge()

    matched = asyncio.run(followup.find_and_apply_instant_followup(
        _CHAN, "BUY", _PARSED, "19830", bridge,
    ))

    assert matched is True
    assert _trade("t-fresh")["stop_loss"] == 4586.0


# ── the repo bound itself ────────────────────────────────────────────────────

def test_repo_honours_the_cutoff_boundary(fresh_db):
    """Trades at or after the cutoff are eligible; older ones are not."""
    now = time.time()
    _insert_trade("t-inside", age_s=_timeout() - 5)
    assert trade_repo.find_latest_instant_trade(_CHAN, now - _timeout()) is not None

    with db.db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET open_time=? WHERE trade_id=?",
                     (now - _timeout() - 5, "t-inside"))
    assert trade_repo.find_latest_instant_trade(_CHAN, now - _timeout()) is None


def test_repo_still_prefers_the_most_recent_eligible_trade(fresh_db):
    now = time.time()
    _insert_trade("t-older", age_s=120, stop_loss=4500.0)
    _insert_trade("t-newer", age_s=10, stop_loss=4400.0)
    row = trade_repo.find_latest_instant_trade(_CHAN, now - _timeout())
    assert db.row_to_dict(row)["trade_id"] == "t-newer"
