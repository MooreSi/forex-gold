"""The balance report's days are the user's days, on any machine.

Owner decision, 2026-09-01 (docs/simon-handover/017 and 020): "it should always
be local time -- if I'm based in the UK use my local time based on the time of
the year, and if there are other users in other countries use their specific
local time."

There are two conversions and both have to use the same clock:

  * where the period boundaries fall, and
  * which side of them a stored trade lands.

The second is the subtler one. Close times are epoch seconds, and
`datetime.fromtimestamp(x)` with no timezone uses the machine's own zone. On a
single-machine install that IS the user's zone and the two agree -- which is
why this went unnoticed. On a VPS given an explicit offset they do not, and the
buckets become one machine's days wearing another's labels.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.src.services.positions import core_bot_balance_report as rep
from backend.src.services.risk import clock as trading_clock


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
        assert seen[0] == pytest.approx(trading_clock.to_timestamp(
            datetime(2026, 7, 1, 0, 0)))

    def test_the_cutoff_is_the_earlier_of_week_and_month_start(
            self, closed_trades, monkeypatch):
        """A week spanning a month boundary is why it is a min(), not the
        month start."""
        seen: list = []
        monkeypatch.setattr(rep, "closed_since",
                            lambda cutoff: seen.append(cutoff) or [])

        rep.period_totals(now=datetime(2026, 7, 2, 12, 0))   # Thursday

        assert seen[0] == pytest.approx(trading_clock.to_timestamp(
            datetime(2026, 6, 29, 0, 0)))                    # the Monday


class TestItDoesNotReadTheMachineClock:
    """Structural, and that is not laziness.

    With no offset configured -- the default, and what the test machine runs --
    `datetime.fromtimestamp(x)` and the clock's own conversion return the same
    thing, because both are the machine's zone. So no behavioural test run here
    can tell the fixed code from the broken code; the difference only appears
    on a machine given an explicit offset, which is the VPS and not the machine
    anyone runs tests on.

    Confirmed by mutation: reverting either conversion left every behavioural
    test in this file green. These assertions are what actually hold the
    property.
    """

    def test_no_naive_now_or_fromtimestamp_remains(self):
        """A bare `datetime.now()` or `datetime.fromtimestamp(x)` is the
        machine's zone, whatever the clock is configured to be."""
        import ast
        import pathlib

        src = pathlib.Path(rep.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        offenders = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            owner = getattr(node.func.value, "id", "")
            if owner not in ("datetime", "dt", "_dt"):
                continue
            if node.func.attr == "now" and not node.args:
                offenders.append(f"datetime.now() at line {node.lineno}")
            if node.func.attr == "fromtimestamp" and len(node.args) == 1:
                offenders.append(f"datetime.fromtimestamp(x) at line {node.lineno}")

        assert offenders == [], (
            f"the report is reading the machine's own clock again: {offenders}"
        )

    def test_the_query_window_is_built_on_the_trading_clock(self):
        """`cutoff` is trading-clock wall time, so `cutoff.timestamp()` always
        reads it as the machine's zone. Identical here; wrong on a VPS."""
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
        assert (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == "to_timestamp"), (
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

        # `datetime.now()` only -- `_clock.now()` is the fix, not the fault.
        naive_now = [c.lineno for c in ast.walk(fn)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                     and c.func.attr == "now" and not c.args
                     and getattr(c.func.value, "id", "") in ("datetime", "dt")]
        assert naive_now == [], f"cmd_report reads the machine clock at {naive_now}"

        attrs = {c.func.attr for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
        assert "now" in attrs and "to_timestamp" in attrs
