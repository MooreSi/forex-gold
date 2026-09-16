"""The breakout engine's H4 bias was "neutral" on 122 of 122 closed signals.

`docs/todo/bugs/060`. Not a tie, not a quiet market: the indicator had never
once evaluated an EMA.

    breakout_signal_service.py:216   get_candles("H4", 40)
    market/indicators.py:23          if len(h4_candles) < 52: return "neutral"

40 < 52, so `compute_h4_bias` returned its starvation fallback every time.
The fallback is indistinguishable from a real neutral reading, which is what
let it run unnoticed from the day the feature was written.

What it silently disabled:

  * `require_dual_bias` defaults to **1.0** and gates four sites with
    `h4_bias in ("bullish", "neutral")`. "neutral" is in the allow-list for
    BOTH directions, so a permanently-neutral H4 made the strict setting
    vacuous. The engine reported a filter that had never rejected anything.
  * `ml_engine`'s `dual_bias` feature was a constant across all 4,894 rows,
    carrying zero information into the model.

The fix names the warmup requirement once and makes every caller honour it,
because three copies of "52" in two packages is how this happened.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from backend.src.services.market import indicators

REPO = pathlib.Path(__file__).resolve().parents[2]


def _candles(closes):
    return [{"close": c, "high": c, "low": c, "open": c} for c in closes]


class TestTheIndicatorItself:
    def test_it_still_refuses_to_guess_when_starved(self):
        """The fallback is correct behaviour. Feeding it too little was the
        bug, not the fallback itself."""
        rising = [100.0 + i for i in range(indicators.H4_BIAS_MIN_CANDLES - 1)]
        assert indicators.compute_h4_bias(_candles(rising)) == "neutral"

    def test_with_the_warmup_met_a_rising_market_reads_bullish(self):
        rising = [100.0 + i for i in range(indicators.H4_BIAS_MIN_CANDLES)]
        assert indicators.compute_h4_bias(_candles(rising)) == "bullish"

    def test_with_the_warmup_met_a_falling_market_reads_bearish(self):
        falling = [500.0 - i for i in range(indicators.H4_BIAS_MIN_CANDLES)]
        assert indicators.compute_h4_bias(_candles(falling)) == "bearish"

    def test_the_warmup_constant_matches_what_the_ema_needs(self):
        """EMA50 needs 50 periods plus warmup. The constant exists so the
        callers and this test read one number instead of copying it."""
        assert indicators.H4_BIAS_MIN_CANDLES >= 52


class TestEveryCallerFeedsItEnough:
    """The class-level guard. Fixing the two known sites is worth nothing if
    the next caller asks for 40 again.

    Scoped to the modules that actually call `compute_h4_bias`, and parsed as
    AST rather than grepped: the first version of this scanner was a regex
    over every file in `backend/`, which swept in the reversal engine's
    deliberate 6-bar fetch (a different function, `get_htf_bias`, documented
    to read `h4_candles[-6:]`) and then matched the number inside a comment
    that described the bug. A scanner that reports its own documentation is
    not a scanner.
    """

    def _modules_computing_h4_bias(self):
        out = []
        for path in sorted((REPO / "backend").rglob("*.py")):
            if "__pycache__" in path.parts or path.name == "indicators.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            calls = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                     and n.func.id == "compute_h4_bias"]
            if calls:
                out.append((path.relative_to(REPO).as_posix(), tree))
        return out

    def _h4_literal_fetches(self, tree):
        """`get_candles("H4", <int literal>)` -- a Name or expression is fine,
        because those resolve through H4_BIAS_MIN_CANDLES."""
        out = []
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "get_candles" and len(n.args) >= 2):
                continue
            tf, count = n.args[0], n.args[1]
            if not (isinstance(tf, ast.Constant) and tf.value == "H4"):
                continue
            if isinstance(count, ast.Constant) and isinstance(count.value, int):
                out.append((n.lineno, count.value))
        return out

    def test_there_are_modules_to_check(self):
        """Negative control: a scanner that finds no modules asserts nothing."""
        assert self._modules_computing_h4_bias()

    def test_no_h4_fetch_asks_for_less_than_the_warmup(self):
        starved = []
        for rel, tree in self._modules_computing_h4_bias():
            for line, count in self._h4_literal_fetches(tree):
                if count < indicators.H4_BIAS_MIN_CANDLES:
                    starved.append(f"{rel}:{line} asks for {count}")
        assert starved == []

    def test_the_resolved_window_constant_clears_the_warmup(self):
        """The literal scan above reads `get_candles("H4", 40)` but cannot
        follow `get_candles("H4", _H4_CANDLES)`. A mutant that set
        `_H4_CANDLES = 40` survived it -- the starvation restored through the
        one spelling the scanner was blind to. Resolve the value."""
        from backend.src.services.breakout_signal import (
            breakout_signal_service as svc)
        assert svc._H4_CANDLES >= indicators.H4_BIAS_MIN_CANDLES

    def test_the_backtest_slices_at_least_the_warmup(self):
        """The backtest sliced `h4[hi4 - 40:hi4]`, so every tuning run this
        engine's adaptive parameters were fitted on ALSO saw a permanently
        neutral H4. Fixing the live path and leaving the backtest starved
        would keep the evidence wrong while the engine was right."""
        src = (REPO / "backend/src/services/breakout_signal/backtest.py"
               ).read_text(encoding="utf-8")
        assert "hi4 - (H4_BIAS_MIN_CANDLES + 8)" in src

    def test_the_scanner_catches_a_planted_starved_fetch(self):
        """Second control: the AST walk really reads the count."""
        tree = ast.parse('async def f(b):\n'
                         '    x = await b.get_candles("H4", 40)\n'
                         '    return compute_h4_bias(x)\n')
        assert self._h4_literal_fetches(tree) == [(2, 40)]

    def test_the_scanner_ignores_a_fetch_for_another_timeframe(self):
        tree = ast.parse('async def f(b):\n'
                         '    x = await b.get_candles("H1", 40)\n'
                         '    return compute_h4_bias(x)\n')
        assert self._h4_literal_fetches(tree) == []


class TestTheGateDoesNotSilentlyComeAlive:
    """Golden rule 3: an upgrade must never change how the system trades.

    `require_dual_bias` was nominally 1.0 and functionally 0.0 for its entire
    life. Repairing the H4 feed without touching it would turn a filter that
    had never rejected a signal into a live one, in the same commit, with no
    decision taken and nobody watching. The default now says what the engine
    has actually been doing; turning it on is a separate, deliberate change
    with a demo session behind it.
    """

    def test_the_default_matches_the_behaviour_the_engine_has_had(self):
        from backend.src.services.breakout_signal import adaptive_params as ap
        assert ap.PARAMS["require_dual_bias"]["default"] == 0.0

    def test_it_is_still_settable_to_strict(self):
        from backend.src.services.breakout_signal import adaptive_params as ap
        assert ap.PARAMS["require_dual_bias"]["max"] == 1.0
