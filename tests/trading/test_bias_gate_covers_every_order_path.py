"""Every path that can place an order must consult the bias gate.

The gate shipped 2026-09-09 (reversal-engine/080, /090) covering the shared
open path (`resolution.resolve_open_trade_params`) and the Reversal Engine.
**Two routes that place real orders bypassed it**, and both say so in their own
comments:

* `instant_entry.py` — "this path never calls resolve_open_trade_params(),
  where the shared gate lives. IME is the fastest path to a live order in the
  app, which makes it the one that most needs the check." It already carries
  its own copies of the session, Trading Schedule and news-blackout gates for
  exactly that reason; the bias gate was the fourth one missing.
* `limit_order_signal.py` — calls `place_pending_order()` / `open_trade()`
  directly.

This is the same shape as bugs/024 (three independent copies of the IME gate,
one route eventually missed) and reversal-engine/080 (a trend filter on one
route and not the other). `20-trading-safety.md` warns about it by name.

**These are structural tests and that is a deliberate, stated limitation.**
The gate's LOGIC is unit-tested in `tests/risk/test_htf_bias_gate.py` against
`governor.htf_bias_blocks` directly. What is pinned here is that each route
CALLS it, and calls it before it places anything — the failure mode being
guarded is a route that quietly grows past the gate, not a wrong answer from
the gate. A source check earns its place only when it asserts ordering and
position rather than mere presence, which is what these do.
"""
from __future__ import annotations

import inspect

import pytest

from backend.src.services.trading import instant_entry, limit_order_signal


def _src(mod) -> str:
    """Source of the module. Fine for presence checks."""
    return inspect.getsource(mod)


def _body(fn) -> str:
    """Source of ONE function, with comments and docstring prose stripped.

    Both precautions were needed, and each was learned the hard way on this
    file's first two runs:

    * Ordering must be measured on the FUNCTION, not the module — these
      modules mention `place_pending_order()` in their module docstrings, so a
      module-wide `index()` finds prose at the top of the file and reports the
      gate as running "after" a call it precedes by seventy lines.
    * Comments must go too — the gate's own explanatory comment names
      `place_pending_order()/open_trade()`, so even inside the right function
      the prose matched before the code did.

    This is exactly the `structural-tests-match-comments` trap: a source check
    that reads its own explanation and passes. Strip the prose and the
    assertion is about code.
    """
    out = []
    for line in inspect.getsource(fn).splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        out.append(line.split("#")[0] if "#" in line else line)
    return "\n".join(out)


class TestInstantMarketEntry:
    """IME is the fastest route to a live order in the app."""

    def test_it_consults_the_gate(self):
        assert "htf_bias_blocks" in _src(instant_entry)

    def test_it_checks_before_it_opens_anything(self):
        """A gate consulted after the order is placed is decoration."""
        src = _body(instant_entry.process_instant_entry)
        gate = src.index("htf_bias_blocks")
        for opener in ("open_trade(", "place_market_order("):
            if opener in src:
                assert gate < src.index(opener), f"gate runs after {opener}"

    def test_it_sits_with_the_other_gates_it_had_to_copy(self):
        """Session, schedule and news are all re-checked here because this
        path skips the shared one. The bias gate belongs in that same block,
        not bolted on somewhere later."""
        src = _body(instant_entry.process_instant_entry)

        assert src.index("check_news_blackout") < src.index("htf_bias_blocks")
        assert src.index("htf_bias_blocks") < src.index("bridge.get_tick()")


class TestTheLimitOrderPath:
    """Pending orders reach the broker via place_pending_order, never through
    the shared open path."""

    def test_it_consults_the_gate(self):
        assert "htf_bias_blocks" in _src(limit_order_signal)

    def test_it_checks_before_the_pending_order_is_placed(self):
        src = _body(limit_order_signal.handle_limit_order_signal)

        assert src.index("htf_bias_blocks") < src.index("place_pending_order(")

    def test_it_checks_before_the_realigned_MARKET_order_too(self):
        """`lk_entry_realignment` turns a breached limit into a market order
        through a different call. Gating only the pending branch would leave
        the realigned one open."""
        src = _body(limit_order_signal.handle_limit_order_signal)

        assert src.index("htf_bias_blocks") < src.index("_open_realigned_market_order(")


class TestTheGateIsTheSharedOne:
    """Not a second implementation. reversal-engine/080's whole point."""

    @pytest.mark.parametrize("mod", [instant_entry, limit_order_signal])
    def test_neither_route_rolls_its_own_comparison(self, mod):
        src = _src(mod)
        for smell in ('== "bearish"', '== "bullish"', "== 'bearish'", "== 'bullish'"):
            assert smell not in src, (
                f"{mod.__name__} compares the bias itself instead of calling "
                f"governor.htf_bias_blocks"
            )

    @pytest.mark.parametrize("mod", [instant_entry, limit_order_signal])
    def test_both_reach_it_through_the_governor(self, mod):
        src = _src(mod)
        assert "governor" in src or "_gov" in src
