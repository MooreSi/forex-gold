"""Every route that reaches the broker must declare whether the trend gate runs.

**Written after the gate missed its third route in one day.**
reversal-engine/080 shipped the higher-timeframe bias gate on "the shared open
path". It was then found to miss Immediate Market Entry and limit orders, which
were patched. On 2026-09-09 it was found to miss `scan_auto_execute` as well —
a fresh Telegram signal opened at market on arrival, which is the ordinary case
and the exact one the file was written about.

Three misses, each found by accident, each after the gate was believed
complete. The IME and schedule gates have the identical history: the schedule
gate was patched into IME on 2026-07-23 and into `scan_auto_execute` on
2026-08-06, for the same reason, in the same place.

**So the guard is not another gate. It is this list.** A new route that reaches
the broker fails this file until someone writes down which side of the line it
is on. That is the only thing that would have caught all three.

**The exemptions are a real position, not a shrug.** A manual order is the
operator overriding the system on purpose, and refusing it because the H1 trend
disagrees would be a surprising thing for a button to do. That reading has NOT
been confirmed by the owner — it is recorded here so the question is visible
rather than implied by absence. See the note at the end of
`docs/todo/reversal-engine/080-no-trend-gate-on-the-telegram-path.md`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "backend" / "src"

# A call that puts an order in front of the broker.
_PLACES_AN_ORDER = re.compile(
    r"await\s+(?:open_trade|open_trade_fn|_real_open_trade)\s*\(|"
    r"await\s+_ea\.place_pending_order\s*\(|"
    r"await\s+self\._main_engine\.open_trade\s*\("
)

# Routes that must consult the bias gate, and why they are automatic.
# Two shapes count as consulting the gate: calling it, or handing the bias to
# `resolve_open_trade_params`, which calls it. open_from_signal does the
# second, and an earlier version of this file failed it for doing so.
_CONSULTS = ("htf_bias_blocks(", "htf_bias=")

GATED = {
    "services/trading/scan_auto_execute.py":
        "a Telegram signal opened at market on arrival — the 2026-09-08 case",
    "services/trading/instant_entry.py":
        "Immediate Market Entry, from a bare direction message",
    "services/trading/limit_order_signal.py":
        "an automatic pending/limit order from a zone signal",
    "services/trading/open_from_signal.py":
        "the shared open path, via resolve_open_trade_params",
    "services/reversal_engine/reversal_engine_live_execute.py":
        "the Reversal Engine's own execution",
}

# Routes that deliberately do NOT consult it, each with the reason.
EXEMPT = {
    "services/trading/manual_market_order.py":
        "the Market Order button — the operator overriding the system on "
        "purpose. Protective limits (the risk halt, max_open_trades) DO apply; "
        "see tests/core/test_manual_order_exemptions.py for that split.",
    "services/trading/manual_limit_order.py":
        "the Create Limit Order button — same reasoning as the market one.",
    "services/trading/bot_trading.py":
        "Telegram bot commands typed by the owner — manual by another route.",
    "services/cluster/sync/server.py":
        "the VPS executing an order the Mac already decided on. Gating here "
        "would apply the VPS's own bias read to the other node's decision.",
}


def _modules_that_place_orders() -> set[str]:
    found = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        body = "\n".join(l for l in text.splitlines()
                         if not l.strip().startswith("#"))
        if _PLACES_AN_ORDER.search(body):
            found.add(path.relative_to(SRC).as_posix())
    return found


class TestTheListIsComplete:
    def test_no_route_reaches_the_broker_undeclared(self):
        """The whole point. A new order path fails here until someone decides
        whether the trend gate applies to it."""
        undeclared = _modules_that_place_orders() - set(GATED) - set(EXEMPT)

        assert undeclared == set(), (
            "these modules place orders and are in neither GATED nor EXEMPT: "
            f"{sorted(undeclared)} — decide which, and say why"
        )

    def test_the_list_has_not_gone_stale(self):
        """A declared module that no longer places orders should be removed,
        or the list slowly stops describing the code."""
        places = _modules_that_place_orders()
        gone = (set(GATED) | set(EXEMPT)) - places

        assert gone == set(), (
            f"declared but no longer places orders: {sorted(gone)}"
        )


class TestTheGatedRoutesActuallyConsultIt:
    @pytest.mark.parametrize("rel", sorted(GATED), ids=lambda r: r.split("/")[-1])
    def test_the_gate_is_called(self, rel):
        text = (SRC / rel).read_text(encoding="utf-8", errors="replace")
        body = "\n".join(l for l in text.splitlines()
                         if not l.strip().startswith("#"))

        assert any(k in body for k in _CONSULTS), (
            f"{rel} places orders and is listed as GATED, but neither calls "
            f"htf_bias_blocks nor passes htf_bias= into "
            f"resolve_open_trade_params — {GATED[rel]}"
        )


class TestTheExemptionsAreDeliberate:
    @pytest.mark.parametrize("rel", sorted(EXEMPT), ids=lambda r: r.split("/")[-1])
    def test_each_exemption_carries_a_reason(self, rel):
        assert len(EXEMPT[rel]) > 40, f"{rel} is exempt with no real reason given"

    @pytest.mark.parametrize("rel", sorted(EXEMPT), ids=lambda r: r.split("/")[-1])
    def test_an_exempt_route_has_not_quietly_gained_the_gate(self, rel):
        """If one grows the gate, it is no longer exempt and the list should
        say so — otherwise the list and the code disagree silently, which is
        how the three misses survived."""
        text = (SRC / rel).read_text(encoding="utf-8", errors="replace")
        body = "\n".join(l for l in text.splitlines()
                         if not l.strip().startswith("#"))

        assert not any(k in body for k in _CONSULTS), (
            f"{rel} now consults the gate but is listed EXEMPT — move it to GATED"
        )


class TestWhatTheAuditAlsoFound:
    """Two results from auditing every route against every gate, pinned so
    neither has to be worked out again.
    """

    def test_the_limit_path_is_schedule_gated_at_FILL_not_at_placement(self):
        """`limit_order_signal` consults neither the schedule nor the news
        blackout, and that is NOT a hole.

        A resting order is accepted long before it fills, so the entry gates
        cannot be evaluated at placement. `ea_bridge/_events.py` re-checks the
        schedule when the order actually fills and closes the position if the
        window has shut. This test exists because the limit path looks like a
        fourth uncovered route until you find that check.
        """
        events = (SRC / "services/broker/ea_bridge/_events.py").read_text(
            encoding="utf-8", errors="replace")

        assert "check_trading_schedule(" in events, (
            "the pending-order fill path no longer re-checks the trading "
            "schedule, which is the only thing gating a resting order"
        )
        assert "trading_schedule_blocked" in events, (
            "the schedule check no longer closes the position it blocked"
        )

    def test_and_the_news_blackout_is_NOT_checked_there(self):
        """The other half of the same argument is missing — bugs/040.

        This asserts the CURRENT behaviour, deliberately. It is not an
        endorsement: it is a tripwire, so that if someone adds the news check
        they are sent to bugs/040 to record the decision rather than leaving
        the two gates silently asymmetric in the other direction.
        """
        events = (SRC / "services/broker/ea_bridge/_events.py").read_text(
            encoding="utf-8", errors="replace")

        assert "check_news_blackout(" not in events, (
            "a news check appeared on the pending-order fill path — that is "
            "bugs/040 and it needs the owner's decision recorded there"
        )
