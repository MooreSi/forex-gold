"""The resting-order sweep must not stop when the Reversal Engine is stopped.

reversal-engine/050 withdraws a resting order when the higher-timeframe bias
turns against it. `revalidate_resting_orders` sweeps
`fetch_working_pending_orders()` — **every** working order, including Limit
Runner orders placed from a Telegram signal, which have nothing to do with the
Reversal Engine.

But it was invoked from `ReversalEngine._check_outcomes`, so its lifetime was
the engine's: `_cycle_loop` runs `while self.is_running`, and the owner can
stop that engine from the panel (`re_user_stopped`). Stopping it also silently
stopped revalidating Telegram resting orders.

There is a second, quieter coupling in the same place. `_check_outcomes`
returns early when there is no tick or the price is zero, so the sweep was
skipped then too.

**Same shape as the trend gate missing `scan_auto_execute`**: a protection that
covers every route, reachable only through one of them.

The sweep now runs from the monitor cycle, which is where the other
cross-cutting sweeps already live (equity protect, basket harvest, orphan
reconcile) and which keeps the same once-a-minute throttle the orphan sweep
uses. The swept function itself is unchanged.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from backend.src.services.positions import monitor_cycle


class TestTheMonitorCycleOwnsIt:
    def test_the_cycle_calls_the_sweep(self):
        body = "\n".join(
            l for l in inspect.getsource(monitor_cycle).splitlines()
            if not l.strip().startswith("#")
        )

        assert "revalidate_resting_orders" in body, (
            "the monitor cycle does not sweep resting orders, so the sweep "
            "still depends on the Reversal Engine being started"
        )

    def test_it_has_its_own_throttle_field(self):
        assert hasattr(monitor_cycle.MonitorState(), "last_resting_sweep")

    def test_the_sweep_is_not_inside_the_open_trades_block(self):
        """The case that matters most is resting orders with NOTHING open — a
        limit order waiting for price. If the call sits inside
        `if open_trades:` it never runs then, which is most of the time."""
        src = inspect.getsource(monitor_cycle.run_monitor_cycle)
        lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
        # The THROTTLE line, not the call: the call sits inside a local import
        # block indented several levels deeper, and measuring that instead
        # failed a correct implementation on the first attempt.
        call = next(i for i, l in enumerate(lines)
                    if "last_resting_sweep >" in l)
        guard = next(i for i, l in enumerate(lines) if "if open_trades and" in l)
        guard_indent = len(lines[guard]) - len(lines[guard].lstrip())
        call_indent = len(lines[call]) - len(lines[call].lstrip())

        assert call_indent <= guard_indent, (
            "the resting sweep sits inside the open-trades block, so it never "
            "runs when only resting orders exist"
        )


class TestTheReversalEngineNoLongerOwnsIt:
    def test_check_outcomes_does_not_drive_the_sweep(self):
        from backend.src.services.reversal_engine import reversal_engine_service as res

        body = "\n".join(
            l for l in inspect.getsource(res.ReversalEngine._check_outcomes).splitlines()
            if not l.strip().startswith("#")
        )

        assert "revalidate_resting_orders" not in body, (
            "the engine still drives the sweep, so stopping it stops "
            "protecting Telegram resting orders"
        )
