"""Which clock the trading schedule is read against.

This pins current behaviour rather than asserting it is right, because the
answer is a policy decision and it is not written down anywhere. Raised as
`docs/simon-handover/017`.

The facts, as of 2026-09-01:

  * **Sessions** (`get_session`, Asia/London/NY, the news windows, the
    counter-bias windows) are evaluated in **UTC**, explicitly.
  * **The trading schedule** (`check_trading_schedule`,
    `get_schedule_strategy_override`) is evaluated with a bare
    `datetime.now()`, which is **each machine's local time**.

Both are defensible on their own. Together they mean "09:00" means two
different instants depending on which gate is reading it — and the schedule is
**mirrored between the Mac and the VPS by the sync link**, so a schedule set on
one runs at that machine's wall clock on the other. A 09:00–12:00 window set on
a UK Mac is 04:00–07:00 on a US-East VPS, gating a different part of the
trading day entirely.

These tests exist so that whichever way it is settled, the change is deliberate
and visible rather than a one-word edit nobody notices.
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from backend.src.services.risk import schedule

REPO = pathlib.Path(__file__).resolve().parents[2]


def _now_calls_without_tz(path: pathlib.Path, fn_names) -> list:
    """Every `datetime.now()` with no timezone inside the named functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in fn_names):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "now"
                    and not inner.args and not inner.keywords):
                out.append((node.name, inner.lineno))
    return out


class TestTheScheduleUsesTheMachinesLOCALClock:
    """Pinned, not endorsed. See docs/simon-handover/017."""

    def test_the_schedule_gate_reads_a_naive_now(self):
        found = _now_calls_without_tz(
            REPO / "backend/src/services/risk/schedule.py",
            {"check_trading_schedule", "get_schedule_strategy_override"},
        )

        assert found, (
            "the trading schedule no longer reads a bare datetime.now(). If it "
            "was moved to UTC deliberately, that answers "
            "docs/simon-handover/017 -- update this test and that note "
            "together, and check the two nodes agree."
        )

    def test_a_caller_may_pass_its_own_clock(self):
        """The `now` parameter is how any change here would be made testable,
        and it is already honoured."""
        far_future = datetime(2030, 1, 1, 3, 0)

        allowed, _reason = schedule.check_trading_schedule(now=far_future)

        assert isinstance(allowed, bool)


class TestSessionsUseUTC:
    """The other half of the inconsistency, asserted so the difference is
    visible in one place."""

    def test_get_session_is_explicitly_utc(self):
        from backend.src.services.test_signal import signal_generator as sg

        src = pathlib.Path(
            REPO / "backend/src/services/test_signal/signal_generator.py"
        ).read_text(encoding="utf-8")
        body_start = src.index("def get_session()")
        body = src[body_start:body_start + 400]

        assert "timezone.utc" in body, (
            "sessions moved off UTC; the schedule/session clock question in "
            "docs/simon-handover/017 needs revisiting"
        )

    def test_the_counter_bias_windows_are_utc_too(self):
        from backend.src.services.test_signal import signal_indicators as si

        src = pathlib.Path(
            REPO / "backend/src/services/test_signal/signal_indicators.py"
        ).read_text(encoding="utf-8")
        start = src.index("def _counter_bias_allowed")
        assert "timezone.utc" in src[start:start + 800]


class TestWhyItMatters:
    """The concrete consequence, expressed as arithmetic rather than prose."""

    @pytest.mark.parametrize("offset_hours,label", [
        (0, "UK winter"), (-5, "US East"), (8, "Singapore"),
    ])
    def test_the_same_window_covers_a_different_utc_span_per_machine(
            self, offset_hours, label):
        window_start_local = 9        # what the operator typed
        as_utc = (window_start_local - offset_hours) % 24

        if offset_hours == 0:
            assert as_utc == 9
        else:
            assert as_utc != 9, (
                f"a 09:00 window on a machine at UTC{offset_hours:+d} ({label}) "
                f"starts at {as_utc:02d}:00 UTC -- the schedule is mirrored "
                f"between nodes, so the two would gate different hours"
            )
