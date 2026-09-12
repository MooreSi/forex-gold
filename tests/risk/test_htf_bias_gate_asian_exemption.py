"""The trend gate is wrong in the Asian session, and may be stood down there.

**Nothing in this file reaches a broker.** Every test calls pure functions
with dicts, or reads source text; no bridge is constructed and no order
call exists to reach.

## What was measured

`governor.htf_bias_blocks` exists because trading WITH the higher-timeframe
bias was the only profitable group on the account -- see
`test_htf_bias_gate.py` for that evidence and the 2026-09-08 post-mortem
behind it. That measurement was taken over the whole clock. Split by
session, it inverts:

| session | with the bias | against it |
|---|---|---|
| asian (00-07 UTC) | n=693, **-$6.26**, CI [-9.40, -3.12] | n=621, -$0.50, CI [-3.69, 2.69] |
| every other | n=1,480, -$1.02, CI [-3.30, 1.27] | n=1,154, **-$4.81**, CI [-7.48, -2.14] |

Measured 2026-09-12 over all 5,414 `re_signals` rows. Both bolded cells hold
their sign across chronological halves (-6.58/-5.94 and -5.37/-4.25);
neither of the other two does anything but straddle zero.

Outside Asia the gate refuses the cohort that loses $4.81 a trade, which is
what it is for. Inside Asia it refuses the cohort that loses nothing and
admits the one that loses $6.26 -- about $5.76 a trade across 1,314 signals,
pointing the wrong way.

**This exempts; it does not invert.** Asian counter-trend is -$0.50 with an
interval straddling zero. That is "no evidence of an edge", not an edge, and
a rule that went on to PREFER counter-trend trades in Asia would be reading
a straddling interval as a signal -- the exact mistake the AI tuner declined
to make on 2026-09-11.

## Why it is a capability gate and not an argument to `htf_bias_blocks`

Two reasons, and the second is the one that decided it.

1. `htf_bias_blocks` is shared by six order routes and the evidence above is
   Reversal Engine signals only. A switch read inside it is a switch that
   silently reaches five routes whose Asian numbers nobody has looked at.

2. **The Reversal Engine's live path refuses a counter-bias trade in TWO
   places**, not one: the shared gate, and the original `level_score < 0.75`
   bypass beside it (reversal-engine/090). While the gate is on the second
   is a strict subset of the first, so today it changes no outcome -- but
   stand the first one down in Asia and the second takes over, and the
   switch would quietly apply to high-scoring levels only. Both have to
   stand down together, which means the exemption is a decision made at that
   call site, above both rules.

So `capability_gates.asian_bias_exempt` answers "does this session opt out
of the trend rule at all", `governor.htf_bias_blocks` keeps its signature
and all six callers, and the two other engines are untouched.

**An open question for the owner.** The Bounce engine holds the OPPOSITE
belief: `test_signal/test_signal_generate.py` blocks counter-bias signals in
the Asian session specifically, and has since before this was measured. Two
engines with opposite rules about the same hours is a thing to settle, not
to leave implicit. Recorded in docs/simon-handover/.

**Default OFF** (rules/60-adding-a-tunable): with the column absent or 0,
every one of these paths behaves exactly as it does today, and this has
never been demoed.
"""
from __future__ import annotations

import inspect
import re

import pytest

from backend.src.services.risk import capability_gates as caps
from backend.src.services.risk import governor


def _rs(gate=1, exempt=1):
    return {"htf_bias_gate_enabled": gate,
            "htf_bias_asian_exempt": exempt}


class TestTheDefaultChangesNothing:
    """rules/60: a new setting's default is byte-identical to the behaviour
    it replaces."""

    def test_the_column_absent_exempts_nothing(self):
        """An install that has not run migration 45 yet."""
        assert caps.asian_bias_exempt({"htf_bias_gate_enabled": 1},
                                      "asian") is False

    def test_an_entirely_empty_settings_row_exempts_nothing(self):
        assert caps.asian_bias_exempt({}, "asian") is False

    def test_explicitly_off_exempts_nothing(self):
        assert caps.asian_bias_exempt(_rs(exempt=0), "asian") is False


class TestWhenItExempts:
    @pytest.mark.parametrize("spelling", ["asian", "ASIAN", " Asian "])
    def test_on_and_in_asia(self, spelling):
        assert caps.asian_bias_exempt(_rs(), spelling) is True

    @pytest.mark.parametrize("session", ["london", "overlap", "ny", "off"])
    def test_on_but_outside_asia(self, session):
        """The measurement that justifies the exemption is confined to
        00-07 UTC, and so is the exemption. Outside it the gate refuses the
        cohort losing $4.81 a trade, which is the whole reason it exists."""
        assert caps.asian_bias_exempt(_rs(), session) is False

    @pytest.mark.parametrize("not_asia", ["", None, "asia", "unknown"])
    def test_a_caller_that_does_not_know_its_session(self, not_asia):
        """`level_detector.get_session` returns exactly one of asian /
        london / overlap / ny / off. Anything else is a caller that cannot
        say what session it is in, and not-knowing keeps the gate on.

        "asia" is in this list on purpose: it is the plausible typo, and a
        match on it would mean two spellings of one session with only one of
        them tested."""
        assert caps.asian_bias_exempt(_rs(), not_asia) is False

    def test_the_gate_being_off_exempts_nothing(self):
        """Load-bearing, and not merely tidy. With the trend gate off, the
        Reversal Engine still applies the original `level_score < 0.75`
        counter-bias bypass, and the call site stands that down on this
        answer too. An exemption that returned True here would switch off a
        rule that predates the gate and was never part of this change."""
        assert caps.asian_bias_exempt(_rs(gate=0), "asian") is False


class TestTheSharedGateIsUntouched:
    """Six order routes call `htf_bias_blocks`. This change must not reach
    five of them."""

    def test_its_signature_is_unchanged(self):
        sig = inspect.signature(governor.htf_bias_blocks)

        assert list(sig.parameters) == ["direction", "htf_bias", "rs"]

    def test_it_still_refuses_a_counter_bias_trade_with_the_switch_on(self):
        """Even the Asian exemption's own settings row does not reach it --
        the function has no idea the switch exists."""
        assert governor.htf_bias_blocks("BUY", "bearish", _rs()) is not None
        assert governor.htf_bias_blocks("SELL", "bullish", _rs()) is not None

    def test_it_does_not_read_the_new_switch(self):
        assert "htf_bias_asian_exempt" not in inspect.getsource(
            governor.htf_bias_blocks)


class TestOnlyTheMeasuredRouteOptsIn:
    """A structural pin, because this is the property the docstring claims
    and nothing else can check it: the exemption reaches the Reversal Engine
    and no other order route."""

    _RE_ROUTE = ("backend.src.services.reversal_engine"
                 ".reversal_engine_live_execute")
    _OTHER_ROUTES = (
        "backend.src.services.trading.instant_entry",
        "backend.src.services.trading.limit_order_signal",
        "backend.src.services.trading.scan_auto_execute",
        "backend.src.services.trading.resting_revalidation",
        "backend.src.services.signals.resolution",
    )

    _CALL = re.compile(r"asian_bias_exempt\(")

    @staticmethod
    def _src(module_name: str) -> str:
        import importlib
        return inspect.getsource(importlib.import_module(module_name))

    def test_the_reversal_engine_consults_it(self):
        assert self._CALL.search(self._src(self._RE_ROUTE))

    def test_it_stands_down_both_of_that_paths_counter_bias_rules(self):
        """The whole reason this is a call-site decision. If the exemption
        only suppressed `htf_bias_blocks`, the `level_score < 0.75` bypass
        would still refuse counter-bias signals on weak levels in Asia, and
        the switch would silently mean "high-scoring levels only"."""
        src = self._src(self._RE_ROUTE)
        body = "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith("#"))

        # Both rules still exist ...
        assert "_gov.htf_bias_blocks(" in body
        assert "level_score < 0.75" in body
        # ... and the exemption is consulted before either is applied.
        exempt_at = body.index("asian_bias_exempt(")
        assert exempt_at < body.index("_gov.htf_bias_blocks(")
        assert exempt_at < body.index("level_score < 0.75")

    @pytest.mark.parametrize("module", _OTHER_ROUTES)
    def test_no_other_route_consults_it(self, module):
        """Not a style rule. Consulting it here would extend a Reversal
        Engine measurement to a route whose Asian-session numbers nobody has
        gathered."""
        assert not self._CALL.search(self._src(module)), (
            f"{module} opts into the Asian exemption on evidence that was "
            f"never gathered for it")

    def test_the_scan_can_actually_see_a_call(self):
        """Negative control. Both assertions above are satisfied by a regex
        that matches nothing at all."""
        assert self._CALL.search("if _caps.asian_bias_exempt(rs, s):")
        assert not self._CALL.search("if _caps.atr_barrier_config(rs):")


class TestTheSwitchIsReachable:
    """A switch nobody can turn on is not a switch -- migration 41 shipped
    fourteen of those on 2026-09-11 and had to be followed by a card."""

    def test_a_migration_adds_the_column_defaulting_to_off(self):
        from backend.migrations.steps import MIGRATIONS

        # Some steps are callables rather than statement lists, so this
        # walks only the ones that are iterable.
        adds = [stmt
                for _n, _t, step in MIGRATIONS
                if isinstance(step, (list, tuple))
                for stmt in step
                if isinstance(stmt, str)
                and "ADD COLUMN htf_bias_asian_exempt" in stmt]

        assert len(adds) == 1, adds
        assert "vantage_risk_settings" in adds[0]
        assert "DEFAULT 0" in adds[0]

    def test_the_capabilities_card_reads_and_writes_it(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2]
               / "frontend/pages/reversal_panel/_capabilities.py"
               ).read_text(encoding="utf-8")

        assert 'rs.get("htf_bias_asian_exempt"' in src
        assert '"htf_bias_asian_exempt":' in src

    def test_the_card_says_the_trend_gate_has_to_be_on_first(self):
        """The switch does nothing on its own, exactly like "Ask the
        meta-labeller" before the model is armed. A card that does not say
        so invites the owner to turn it on and conclude it did nothing."""
        from pathlib import Path

        src = (Path(__file__).resolve().parents[2]
               / "frontend/pages/reversal_panel/_capabilities.py"
               ).read_text(encoding="utf-8")
        at = src.index("htf_bias_asian_exempt")
        near = src[at:at + 1600].lower()

        assert "only trade with the trend" in near
