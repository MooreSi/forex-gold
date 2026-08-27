"""The anti-compounding revert in PendingWatcher.

When Immediate Market Entry fires on a gapped signal, the watcher rewrites the
signal's levels to the gap-adjusted ones before attempting activation. If that
activation then fails, it must put the ORIGINAL levels back.

Without that second half, each cycle re-measured the gap from the
already-shifted levels and shifted again, compounding every pass. The comment
in the source records what that cost live: one signal's stop walked 110 pips
over 80 passes, and three of four signals expired without ever opening, at
levels bearing no relation to what the channel sent.

The expiry cap is covered by test_pending_activation_retry_cap. The revert was
not covered at all, and it is about to move into the signals repo, so it is
pinned here first.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.services.signals import pending_activation as pa


ORIGINAL = {"entry_low": 4063.0, "entry_high": 4066.0, "stop_loss": 4071.5}


@pytest.fixture
def pending_signal(fresh_db):
    pa._ACTIVATION_FAILURES.clear()
    with fresh_db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("sig-1", "Reversal Engine", "SELL", ORIGINAL["entry_low"],
             ORIGINAL["entry_high"], ORIGINAL["stop_loss"], "pending", time.time()),
        )
    yield fresh_db
    pa._ACTIVATION_FAILURES.clear()


class _Tick:
    """Price has run BELOW the SELL zone (4063-4066). That is the whole
    premise of the gap-fire: the signal never fills because price left
    without it. gap = entry_low - bid = 2.0 pts, inside MAX_GAP_FIRE_PTS."""
    ask = 4061.2
    bid = 4061.0
    spread_points = 20.0


def _levels():
    with db.db() as conn:
        row = conn.execute(
            "SELECT entry_low, entry_high, stop_loss FROM vantage_signals "
            "WHERE signal_id='sig-1'").fetchone()
    return {"entry_low": row[0], "entry_high": row[1], "stop_loss": row[2]}


def _drive(monkeypatch, *, fail, cycles=1):
    async def _activate(*a, **kw):
        if fail:
            raise RuntimeError("EA rejected")
        return {"trade_id": "t-1"}

    monkeypatch.setattr(pa, "open_trade_from_signal", _activate)
    # Price is outside the zone -- the condition that makes gap-fire apply
    # at all. Patching this True is what makes the whole block dead code.
    monkeypatch.setattr(pa, "price_in_entry_range", lambda *a, **kw: False)
    monkeypatch.setattr(pa, "_ime_enabled_for_source", lambda *a, **kw: True)
    monkeypatch.setattr(pa.telegram_alerts, "send_message",
                        lambda *a, **kw: asyncio.sleep(0))
    rs = {"max_open_trades": 10, "trade_strategy": "scale_out"}
    retry_after: dict = {}
    for _ in range(cycles):
        retry_after.clear()
        asyncio.run(pa.try_activate_pending_signals(_Tick(), rs, object(), retry_after, []))


def test_a_failed_activation_leaves_the_original_levels(monkeypatch, pending_signal):
    """The property. Whatever the watcher did to the levels on the way in, a
    failure has to leave the row as the channel sent it."""
    _drive(monkeypatch, fail=True, cycles=1)
    assert _levels() == pytest.approx(ORIGINAL)


def test_repeated_failures_do_not_compound_the_drift(monkeypatch, pending_signal):
    """The live incident, directly: eighty passes walked a stop 110 pips
    because each cycle re-measured from the previous cycle's shifted levels."""
    _drive(monkeypatch, fail=True, cycles=10)

    levels = _levels()
    assert levels == pytest.approx(ORIGINAL), (
        f"levels drifted across cycles: {levels} vs {ORIGINAL}"
    )


# A third test -- "a successful activation does not revert" -- was dropped
# rather than propped up. Driving the success path needs open_trade_from_signal
# to return a fully-shaped result, and the version I wrote was asserting the
# shape of my own fake more than the behaviour of the code. The two tests above
# are the property that matters: the levels the channel sent survive a failure.
