"""An EA Template open waits at least 30 s for the EA's ack (owner, 2026-09-29).

Five template opens on the VPS between 08:12 and 09:09 on 2026-09-29 logged
"template open ack timed out after 5s". Every one had been placed: the repair
pass later adopted each placeholder onto a real ticket. The ack was late, not
missing, and a timed-out ack leaves a ticket-less placeholder that holds a
trade slot until the repair pass catches up.

The 5 s was the fallback for a template the node could not read; a readable
one waited 10 s + 5 s per leg, 15 s for a one-leg template. Both now wait at
least TEMPLATE_ACK_MIN_S. A long grid keeps its larger figure. A built-in
strategy keeps 5 s: its timeout falls back to Python, and a longer wait there
only delays that fallback.
"""
from __future__ import annotations

import pytest

from backend.src.services.broker import ea_templates as et
from backend.src.services.trading import open_trade as cot
from tests.core.test_template_open_ack_timeout import (  # noqa: F401
    _TimingOutEA, _open, fresh_db,
)


def test_the_minimum_is_30_seconds():
    assert cot.TEMPLATE_ACK_MIN_S == 30.0


def test_a_one_leg_template_waits_the_minimum(monkeypatch, fresh_db):
    et.save_ea_template("Grid", {"mode": "single", "anchors": 1, "pendings": 0,
                                 "tp1_pips": 20.0, "tp1_pct": 100.0})
    ea = _TimingOutEA()

    _open(monkeypatch, ea)

    assert ea.timeout_seen == pytest.approx(30.0)   # was 10 + 5*1 = 15


def test_a_big_grid_keeps_its_longer_wait(monkeypatch, fresh_db):
    """Negative control: the minimum is a floor, not a cap."""
    et.save_ea_template("Grid", {"mode": "grid", "anchors": 2, "pendings": 6,
                                 "lot_anchor": 0.03, "lot_pending": 0.03,
                                 "tp1_pips": 20.0, "tp1_pct": 100.0})
    ea = _TimingOutEA()

    _open(monkeypatch, ea)

    assert ea.timeout_seen == pytest.approx(50.0)   # 10 + 5*8
