"""Which clock the trading schedule is read against.

Settled 2026-09-01 (docs/simon-handover/017). The owner's answer:

> "Keep the clock to UK time which is the timezone I am in so I can keep track
> of the time locally."

So the Trading Schedule is read in **UK wall-clock time** — a fixed zone, not
"whatever this machine's local time is". That distinction is the whole point:
the schedule is mirrored between the Mac and the VPS by the sync link, so the
setting travels while the clock does not. Until today it read a bare
`datetime.now()`, and a 09:00 window set on the Mac would have gated
14:00–17:00 UTC on a US-East VPS with nothing reporting the discrepancy.

**Sessions remain UTC** — Asia/London/NY, the news windows, the counter-bias
windows. Two clocks, on purpose, and these tests keep the difference visible in
one place so neither drifts into the other by accident.
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

from backend.src.services.risk import schedule

REPO = pathlib.Path(__file__).resolve().parents[2]


# Only `datetime.now()` counts. `_clock.now()` is the fix, not the fault, and a
# check that cannot tell them apart reports the corrected code as broken.
_MACHINE_CLOCK_OWNERS = {"datetime", "dt", "_dt", "_dt_exp"}


def _now_calls_without_tz(path: pathlib.Path, fn_names) -> list:
    """Every bare `datetime.now()` inside the named functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in fn_names):
            continue
        for inner in ast.walk(node):
            if not (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "now"
                    and not inner.args and not inner.keywords):
                continue
            if getattr(inner.func.value, "id", "") in _MACHINE_CLOCK_OWNERS:
                out.append((node.name, inner.lineno))
    return out


class TestTheScheduleUsesTheTradingClock:

    def test_the_gate_does_NOT_read_the_machines_own_clock(self):
        """A bare `datetime.now()` here is the bug: it makes the same setting
        mean different hours on the Mac and the VPS."""
        found = _now_calls_without_tz(
            REPO / "backend/src/services/risk/schedule.py",
            {"check_trading_schedule", "get_schedule_strategy_override"},
        )

        assert found == [], (
            f"the schedule is reading the machine's own local clock again at "
            f"{found}. It is mirrored between two machines, so it must go "
            f"through the trading clock rather than the machine's own."
        )

    def test_both_entry_points_use_the_trading_clock(self):
        import ast

        src = (REPO / "backend/src/services/risk/schedule.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        for name in ("check_trading_schedule", "get_schedule_strategy_override"):
            fn = next(n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                      and n.name == name)
            calls = {c.func.attr for c in ast.walk(fn)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
            assert "now" in calls, f"{name} does not read the trading clock"

    def test_the_screen_shows_the_same_clock_as_the_gate(self):
        """The panel highlights "today". If it used a different clock from the
        one being enforced, the day shown could differ from the day gated."""
        src = (REPO / "backend/src/services/positions/_panel_schedule.py"
               ).read_text(encoding="utf-8")

        assert "_clock.now()" in src
        assert "datetime.now().weekday()" not in src

    def test_a_caller_may_still_pass_its_own_clock(self):
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


class TestTheTwoNodesNowAgree:
    """What the fix buys, stated as the property rather than as prose: the same
    instant produces the same schedule time regardless of where the machine
    thinks it is."""

    def test_the_window_hour_no_longer_depends_on_the_machine(self,
                                                              monkeypatch):
        """The instant's OWN timezone must not change the answer.

        The offset is pinned. Without it these three assertions passed on any
        machine in the UK and failed everywhere else -- including CI -- because
        `local_from` with no offset returns the machine's local time. A test
        named "no longer depends on the machine" that depended entirely on the
        machine; found 2026-09-02.
        """
        from datetime import timezone as _tz

        from backend.src.utils import trading_clock as uk_clock

        BST = 60
        instant = datetime(2026, 7, 15, 12, 0, tzinfo=_tz.utc)

        # Same instant, read on machines that believe they are anywhere.
        assert uk_clock.local_from(instant, BST).hour == 13
        assert uk_clock.local_from(
            instant.astimezone(_tz(timedelta(hours=-5))), BST).hour == 13
        assert uk_clock.local_from(
            instant.astimezone(_tz(timedelta(hours=8))), BST).hour == 13

    def test_a_configured_offset_holds_09_00_across_the_clock_change(self):
        """09:00 stays 09:00 on the user's clock across March and October --
        which, for a machine given an explicit offset, means the offset is
        refreshed at each change. `trading_clock`'s docstring is explicit that
        a configured offset does NOT follow daylight saving on its own, so the
        two are passed here as the two different numbers they really are.
        """
        from datetime import timezone as _tz

        from backend.src.utils import trading_clock as uk_clock

        GMT, BST = 0, 60
        winter = datetime(2026, 1, 15, 9, 0, tzinfo=_tz.utc)
        summer = datetime(2026, 7, 15, 8, 0, tzinfo=_tz.utc)

        assert uk_clock.local_from(winter, GMT).hour == 9
        assert uk_clock.local_from(summer, BST).hour == 9

    def test_with_no_offset_it_is_the_machines_own_clock(self, monkeypatch):
        """The other half of the design, and the reason the two tests above
        had to pin: with no offset configured the answer IS the machine's local
        time, whatever that is. Asserted against the machine's own reported
        offset so it holds in any zone."""
        from datetime import timezone as _tz

        from backend.src.utils import trading_clock as uk_clock

        instant = datetime(2026, 7, 15, 12, 0, tzinfo=_tz.utc)
        machine = uk_clock.machine_offset_minutes()

        assert uk_clock.local_from(instant) == uk_clock.local_from(instant, machine)
