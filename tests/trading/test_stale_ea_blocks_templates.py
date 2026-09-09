"""A template strategy must not open on a stale EA build.

bugs/033 left this as the owner's call and he took it 2026-09-09.

**Why templates specifically.** An EA Template *is* an EA-native management
definition -- `is_strategy_portable` returns True for every template precisely
because "there's no Python-managed equivalent to fall back to". So the entire
management of a template trade is whatever build is on the chart. Opening one
against a stale `.ex5` means running it under logic this app has already
replaced: on 2026-09-09 that was v1.05, which harvests each position on its own
profit rather than the basket, so a $50 target never fired on three trades
holding $60 between them.

**Why not just make `is_ea_healthy()` false.** That gate also governs the
EA-portable strategies, which DO have a Python fallback. Failing it there would
silently reroute them rather than refuse, which is a bigger change than was
asked for and hides the problem instead of surfacing it.

**Why refusing is the right shape.** The template branch of `open_trade`
already raises rather than falling through when no EA is reachable, for the
same reason: a template with no EA management is not a trade this app knows how
to run. A stale EA is a narrower version of that.

**Unknown is not stale.** `ea_version_ok` is None when there is no EA source to
compare against (a packaged install). Refusing every template there would break
installs that are entirely correct.
"""
from __future__ import annotations

import inspect

import pytest

from backend.src.services.broker import ea_bridge


class _EA:
    def __init__(self, version_ok, version=None):
        self.ea_version_ok = version_ok
        self.ea_version = version


class TestTheDecision:
    def test_a_template_is_refused_on_a_stale_build(self):
        reason = ea_bridge.template_blocked_by_stale_build(
            _EA(version_ok=False, version="1.05"), "template:GD Institutional")

        assert reason is not None
        assert "1.05" in reason

    def test_the_reason_names_the_remedy(self):
        """An operator seeing a refused trade must know what to do. The whole
        of bugs/033 was an instruction that only existed in a log."""
        reason = ea_bridge.template_blocked_by_stale_build(
            _EA(version_ok=False, version="1.05"), "template:X")

        assert "deploy_ea" in reason

    def test_a_template_on_a_current_build_proceeds(self):
        assert ea_bridge.template_blocked_by_stale_build(
            _EA(version_ok=True, version="1.06"), "template:X") is None

    def test_an_unknown_build_does_not_block(self):
        """None means there was no source to compare against, not that the
        build is wrong."""
        assert ea_bridge.template_blocked_by_stale_build(
            _EA(version_ok=None), "template:X") is None

    @pytest.mark.parametrize("strategy", [
        "scale_out", "trail_stop", "conservative", "adaptive_runner",
    ])
    def test_a_NON_template_strategy_is_never_blocked_by_this(self, strategy):
        """Those have a Python fallback. Refusing them here would reroute or
        stop trades this change was not asked to touch."""
        assert ea_bridge.template_blocked_by_stale_build(
            _EA(version_ok=False, version="1.05"), strategy) is None

    def test_no_ea_at_all_is_not_this_functions_problem(self):
        """`open_trade` already raises its own "requires a connected, healthy
        EA" for that, and two errors for one cause is worse than one."""
        assert ea_bridge.template_blocked_by_stale_build(None, "template:X") is None

    def test_a_bridge_that_throws_does_not_block(self):
        """On the order path. It must not turn a read failure into a refused
        trade."""
        class _Boom:
            @property
            def ea_version_ok(self):
                raise RuntimeError("gone")

        assert ea_bridge.template_blocked_by_stale_build(_Boom(), "template:X") is None


class TestItIsWiredIntoTheOrderPath:
    @staticmethod
    def _body():
        from backend.src.services.trading import open_trade as ot

        src = inspect.getsource(ot.open_trade)
        out = []
        for line in src.splitlines():
            st = line.strip()
            if st.startswith("#"):
                continue
            out.append(line.split("#")[0] if "#" in line else line)
        return "\n".join(out)

    def test_open_trade_consults_it(self):
        assert "template_blocked_by_stale_build(" in self._body()

    def test_it_is_checked_before_the_order_is_handed_to_the_ea(self):
        body = self._body()

        assert body.index("template_blocked_by_stale_build(") < body.index("ea_ack"), (
            "the build is checked after the order has already gone to the EA"
        )
