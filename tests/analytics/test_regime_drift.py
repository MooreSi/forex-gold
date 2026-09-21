"""Measuring whether the frozen regime constants are still true -- docs/todo/004.

`backend/src/utils/regime.py` blocks hours and level types with `frozenset`s
fitted to 510 signals from 2026-06-15 to 2026-07-06. Nothing re-reads those
buckets and nothing notices when they drift. This module is the thing that
notices.

It proposes nothing. Phase 1 of 004 is a measurement, and the single most
important thing it has to report is the one the constants cannot: **a blocked
cell produces no trades, so it produces no evidence, so the block can never be
shown to be wrong.** A report that quietly showed `0 trades, $0` for a blocked
hour beside a live hour's real numbers would read as "harmless", which is the
opposite of true. `no_evidence_blocked` exists to stop that.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.src.services.analytics import regime_drift as rd
from backend.src.utils import regime as rg


# ── Fixtures ──────────────────────────────────────────────────────────────────

# 2026-06-15 00:00:00 UTC, a Monday inside the window the constants came from.
_T0 = 1_781_481_600


def _rec(hour: int, pnl: float, level: str = "swing_high", day: int = 0) -> dict:
    """One closed signal. Only the four fields the report reads are set."""
    return {
        "created_at": _T0 + day * 86_400 + hour * 3_600,
        "level_type": level,
        "net_pnl_dollars": pnl,
    }


def _many(hour: int, pnl_each: float, n: int, level: str = "swing_high") -> list[dict]:
    return [_rec(hour, pnl_each, level, day=i) for i in range(n)]


# ── 1. A cell with data is judged against the constant ───────────────────────

def test_a_blocked_hour_that_now_makes_money_is_reported_as_a_contradiction():
    """Hour 12 is in BOUNCE_BLOCKED_HOURS_UTC. If the engine none the less has
    profitable closed trades there -- from a manual order, a different route or
    a period before the block -- the constant and the data disagree, and that
    is the whole reason this report exists."""
    assert 12 in rg.BOUNCE_BLOCKED_HOURS_UTC
    report = rd.hour_drift(_many(12, +40.0, 30), engine="bounce")

    cell = {c.key: c for c in report.cells}["12"]
    assert cell.blocked_today is True
    assert cell.verdict == rd.CONTRADICTS


def test_an_open_hour_that_now_loses_money_is_reported_as_a_contradiction():
    """The mirror. Hour 9 is open for bounce. A cell we allow and that loses is
    the same kind of disagreement pointing the other way."""
    assert 9 not in rg.BOUNCE_BLOCKED_HOURS_UTC
    report = rd.hour_drift(_many(9, -40.0, 30), engine="bounce")

    cell = {c.key: c for c in report.cells}["09"]
    assert cell.blocked_today is False
    assert cell.verdict == rd.CONTRADICTS


def test_an_open_hour_that_makes_money_agrees_with_the_constant():
    report = rd.hour_drift(_many(9, +40.0, 30), engine="bounce")

    assert {c.key: c for c in report.cells}["09"].verdict == rd.AGREES


def test_a_blocked_hour_that_still_loses_agrees_with_the_constant():
    report = rd.hour_drift(_many(12, -40.0, 30), engine="bounce")

    assert {c.key: c for c in report.cells}["12"].verdict == rd.AGREES


# ── 2. A blocked cell with no trades is self-sealing, and must say so ────────

def test_a_blocked_hour_with_no_trades_reports_no_evidence_not_agreement():
    """The test that matters most. Hour 13 is blocked for both engines, so it
    has no trades, so it cannot be shown to be wrong. Calling that 'agrees'
    would launder a block into a finding."""
    assert 13 in rg.BOUNCE_BLOCKED_HOURS_UTC
    report = rd.hour_drift(_many(9, +40.0, 30), engine="bounce")

    assert {c.key: c for c in report.cells}["13"].verdict == rd.NO_EVIDENCE_BLOCKED


def test_an_open_hour_with_no_trades_is_merely_insufficient():
    """The negative control for the test above: the same emptiness means a
    different thing when nothing was suppressing it."""
    assert 3 not in rg.BOUNCE_BLOCKED_HOURS_UTC
    report = rd.hour_drift(_many(9, +40.0, 30), engine="bounce")

    assert {c.key: c for c in report.cells}["03"].verdict == rd.INSUFFICIENT


def test_a_thin_cell_is_insufficient_however_large_its_total():
    """Three trades at +$500 is not an edge, it is three trades. The threshold
    is the same question as split.py's and handover 036's."""
    report = rd.hour_drift(_many(9, +500.0, 3), engine="bounce")

    cell = {c.key: c for c in report.cells}["09"]
    assert cell.trades == 3
    assert cell.verdict == rd.INSUFFICIENT


# ── 3. Arithmetic ────────────────────────────────────────────────────────────

def test_the_cell_totals_the_pnl_and_win_rate_of_its_own_records():
    recs = _many(9, +40.0, 18) + _many(9, -20.0, 6)
    report = rd.hour_drift(recs, engine="bounce")

    cell = {c.key: c for c in report.cells}["09"]
    assert cell.trades == 24
    assert cell.total_pnl == pytest.approx(18 * 40.0 - 6 * 20.0)
    assert cell.win_rate == pytest.approx(75.0)


def test_records_are_bucketed_by_utc_hour_of_creation():
    """The constants are UTC hour-of-day. A record at 23:59 must not leak into
    the next day's hour 0."""
    recs = _many(23, +40.0, 25)
    report = rd.hour_drift(recs, engine="bounce")

    by_key = {c.key: c for c in report.cells}
    assert by_key["23"].trades == 25
    assert by_key["00"].trades == 0


def test_every_hour_of_the_day_appears_exactly_once():
    report = rd.hour_drift(_many(9, +40.0, 30), engine="bounce")

    assert [c.key for c in report.cells] == [f"{h:02d}" for h in range(24)]


# ── 4. The two engines have different constants ──────────────────────────────

def test_the_breakout_engine_is_judged_against_its_own_blocked_hours():
    """Hour 7 is blocked for bounce and open for breakout. Reading one engine's
    data against the other's constants would invent contradictions."""
    assert 7 in rg.BOUNCE_BLOCKED_HOURS_UTC
    assert 7 not in rg.BREAKOUT_BLOCKED_HOURS_UTC
    recs = _many(7, +40.0, 30)

    bounce   = {c.key: c for c in rd.hour_drift(recs, engine="bounce").cells}["07"]
    breakout = {c.key: c for c in rd.hour_drift(recs, engine="breakout").cells}["07"]

    assert bounce.blocked_today is True
    assert breakout.blocked_today is False


def test_an_unknown_engine_is_refused_rather_than_defaulted():
    with pytest.raises(ValueError):
        rd.hour_drift(_many(9, +40.0, 30), engine="setforget")


# ── 5. Level types ───────────────────────────────────────────────────────────

def test_a_blocked_entry_level_that_now_makes_money_is_a_contradiction():
    assert "liq_low" in rg.BOUNCE_BLOCKED_ENTRY_LEVELS
    recs = _many(9, +40.0, 30, level="liq_low")

    report = rd.level_drift(recs, engine="bounce")

    assert {c.key: c for c in report.cells}["liq_low"].verdict == rd.CONTRADICTS


def test_a_level_type_seen_in_the_data_but_in_no_constant_is_still_reported():
    """The constants name the level types that were losing in one window. A
    type that did not exist then must not be invisible now."""
    recs = _many(9, -40.0, 30, level="daily_open")

    report = rd.level_drift(recs, engine="bounce")

    cell = {c.key: c for c in report.cells}["daily_open"]
    assert cell.blocked_today is False
    assert cell.verdict == rd.CONTRADICTS


def test_records_with_no_level_type_are_counted_out_not_silently_dropped():
    recs = _many(9, +40.0, 30, level="liq_low")
    recs += [{"created_at": _T0, "level_type": None, "net_pnl_dollars": 10.0}]

    report = rd.level_drift(recs, engine="bounce")

    assert report.skipped == 1
    assert sum(c.trades for c in report.cells) == 30


# ── 6. The report states its window and proposes nothing ─────────────────────

def test_the_report_carries_the_window_its_numbers_came_from():
    recs = _many(9, +40.0, 30)
    report = rd.hour_drift(recs, engine="bounce")

    assert report.window_from == min(r["created_at"] for r in recs)
    assert report.window_to == max(r["created_at"] for r in recs)


def test_the_report_records_the_window_the_constants_came_from():
    """So the two dates sit beside each other and the staleness is visible
    without anyone having to remember it."""
    assert rd.CONSTANTS_FITTED_TO == ("2026-06-15", "2026-07-06")


def test_no_cell_carries_a_proposed_value():
    """Phase 1 measures. Proposing is phase 2 and needs the owner's answer to
    004 section 4A first, because a proposal that widens a gate can open
    trading hours that are shut today."""
    cell = rd.hour_drift(_many(9, +40.0, 30), engine="bounce").cells[9]

    assert not hasattr(cell, "proposed")
    assert not hasattr(cell, "suggested_value")


def test_an_empty_record_list_reports_nothing_rather_than_agreement():
    report = rd.hour_drift([], engine="bounce")

    assert report.note != ""
    assert all(c.verdict in (rd.INSUFFICIENT, rd.NO_EVIDENCE_BLOCKED)
               for c in report.cells)


# ── 7. Purity ────────────────────────────────────────────────────────────────

def test_regime_drift_imports_nothing_that_can_trade():
    src  = Path(rd.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = ("services.trading", "services.broker", "services.risk",
              "backend.src.db", "MetaTrader5", "mt5")

    seen: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            seen.append(node.module or "")

    for mod in seen:
        assert not any(b in mod for b in banned), f"regime_drift.py imports {mod}"
