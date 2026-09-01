"""The balance report's days are UK days, on any machine.

Owner decision, 2026-09-01, extending docs/simon-handover/017 from the Trading
Schedule to the reports. "Today", "this week" and "this month" mean UK
calendar periods — the same clock the schedule gates on and the one he reads.

There are two conversions and both were using the machine's own zone:

  * `datetime.now()` for where the period boundaries fall, and
  * `datetime.fromtimestamp(close_time)` for which side of them a trade lands.

The second is the subtler one. Trade close times are stored as epoch seconds;
converting them with the machine's zone buckets them into the MACHINE's days
while labelling them UK ones. On a VPS five hours behind, a trade closed at
02:00 UK on Tuesday is reported under Monday.

These tests drive `period_totals` with an explicit `now`, so they assert the
bucketing rather than whatever today happens to be.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.src.services.positions import core_bot_balance_report as rep
from backend.src.utils import uk_clock


def _closed(trade_id, close_epoch, pnl):
    return {"trade_id": trade_id, "close_time": close_epoch, "net_pnl": pnl,
            "status": "closed"}


@pytest.fixture
def closed_trades(monkeypatch):
    box: list = []
    monkeypatch.setattr(rep, "closed_since",
                        lambda cutoff: [t for t in box
                                        if float(t["close_time"]) >= cutoff])
    return box


class TestATradeIsBucketedByItsUKDay:

    def test_a_summer_trade_just_after_uk_midnight_counts_as_the_NEW_day(
            self, closed_trades):
        """23:10 UTC on 15 July is 00:10 UK on the 16th. Bucketing it by UTC —
        or by a machine behind the UK — puts it on the wrong day."""
        closed_trades.append(_closed(
            "t1", datetime(2026, 7, 15, 23, 10, tzinfo=timezone.utc).timestamp(), 30.0))

        totals = rep.period_totals(now=datetime(2026, 7, 16, 12, 0))

        assert totals["today"].pnl == pytest.approx(30.0)

    def test_and_ten_minutes_earlier_counts_as_the_PREVIOUS_day(
            self, closed_trades):
        """22:50 UTC is 23:50 UK on the 15th — the day before."""
        closed_trades.append(_closed(
            "t1", datetime(2026, 7, 15, 22, 50, tzinfo=timezone.utc).timestamp(), 30.0))

        totals = rep.period_totals(now=datetime(2026, 7, 16, 12, 0))

        assert totals["today"].pnl == pytest.approx(0.0)
        assert totals["week"].pnl == pytest.approx(30.0)

    def test_a_winter_trade_uses_the_same_boundary_as_utc(self, closed_trades):
        """In January UK time is UTC, so the two agree — which is why this was
        invisible for half the year."""
        closed_trades.append(_closed(
            "t1", datetime(2026, 1, 15, 23, 10, tzinfo=timezone.utc).timestamp(), 30.0))

        totals = rep.period_totals(now=datetime(2026, 1, 16, 12, 0))

        assert totals["today"].pnl == pytest.approx(0.0)


class TestThePeriodBoundariesThemselves:

    def test_the_week_starts_on_monday(self, closed_trades):
        wednesday = datetime(2026, 7, 15, 12, 0)

        totals = rep.period_totals(now=wednesday)

        assert min(totals["days"]) == datetime(2026, 7, 13, 0, 0)

    def test_the_query_window_starts_at_a_UK_instant(self, closed_trades,
                                                     monkeypatch):
        """The cutoff handed to the database must be the epoch for UK midnight,
        not for the machine's midnight."""
        seen: list = []
        monkeypatch.setattr(rep, "closed_since",
                            lambda cutoff: seen.append(cutoff) or [])

        rep.period_totals(now=datetime(2026, 7, 15, 12, 0))

        assert seen
        # 1 July 2026 00:00 UK == 30 June 23:00 UTC (month start beats week start)
        assert seen[0] == pytest.approx(uk_clock.uk_timestamp(
            datetime(2026, 7, 1, 0, 0)))

    def test_the_cutoff_is_the_earlier_of_week_and_month_start(
            self, closed_trades, monkeypatch):
        """A week spanning a month boundary is why it is a min(), not the
        month start."""
        seen: list = []
        monkeypatch.setattr(rep, "closed_since",
                            lambda cutoff: seen.append(cutoff) or [])

        rep.period_totals(now=datetime(2026, 7, 2, 12, 0))   # Thursday

        assert seen[0] == pytest.approx(uk_clock.uk_timestamp(
            datetime(2026, 6, 29, 0, 0)))                    # the Monday


class TestItDoesNotReadTheMachineClock:
    """Structural, and that is not laziness.

    On a machine that is in the UK, `datetime.fromtimestamp(x)` and
    `uk_from_timestamp(x)` return the same thing — so no behavioural test run
    here can tell the fixed code from the broken code. The same is true on a
    UTC machine in winter, which is what CI is. The difference only appears on
    a machine in another zone, which is precisely the machine nobody runs the
    tests on.

    Confirmed by mutation: reverting either conversion left every behavioural
    test in this file green. These assertions are what actually hold the
    property.
    """

    def test_no_naive_now_or_fromtimestamp_remains(self):
        import ast
        import pathlib

        src = pathlib.Path(rep.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr in ("now", "fromtimestamp") and not node.args:
                offenders.append(f"{node.func.attr}() at line {node.lineno}")
            if node.func.attr == "fromtimestamp" and len(node.args) == 1:
                offenders.append(f"fromtimestamp(x) at line {node.lineno}")

        assert offenders == [], (
            f"the report is reading the machine's own clock again: {offenders}"
        )

    def test_the_query_window_is_built_with_uk_timestamp(self):
        """`cutoff` is UK wall time, so `cutoff.timestamp()` reads it as the
        machine's zone. Behaviourally identical here; wrong on a VPS."""
        import ast
        import pathlib

        src = pathlib.Path(rep.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "period_totals")

        calls = [c for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "closed_since"]
        assert len(calls) == 1, "expected one closed_since call"

        arg = calls[0].args[0]
        assert (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                and arg.func.id == "uk_timestamp"), (
            "closed_since is being given a naive .timestamp(), which starts "
            "the window at the wrong instant on any machine outside the UK"
        )

    def test_the_emailed_report_uses_the_same_clock(self):
        """`cmd_report` builds its own day cutoff and its own date strings."""
        import ast
        import pathlib

        import backend.src.services.trading.bot_trading as bt

        src = pathlib.Path(bt.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "cmd_report")

        naive_now = [c.lineno for c in ast.walk(fn)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                     and c.func.attr == "now" and not c.args]
        assert naive_now == [], f"cmd_report reads the machine clock at {naive_now}"

        names = {c.func.id for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "uk_now" in names and "uk_timestamp" in names
