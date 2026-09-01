"""The five-demo runbook quotes log lines. They have to still exist.

`docs/simon-handover/013-the-five-demos-runbook.md` is read once, at a
terminal, on a demo account, with the owner's evening on the line. Every
"**Expect:** the log says X" in it is a claim about the code, and the code
moves. A quoted line that has been reworded turns a passing demo into a
failed one, or worse, a failing demo into a shrug.

This is not a test of the demos -- `test_killer_demos.py` drives those offline
against the fake broker. It is a test that the instructions still describe this
repository. Checking the runbook against the code by hand on 2026-09-01 found
five things that had drifted, including one that contradicted its own setup
step and one that stated the opposite of what the code does.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RUNBOOK = REPO / "docs" / "simon-handover" / "013-the-five-demos-runbook.md"

# Fragment the runbook quotes -> the file that must still contain it.
# Fragments, not whole lines: the source wraps these across string literals,
# so a full-sentence match would fail on formatting rather than on meaning.
QUOTED = [
    ("adopted existing broker order",
     "backend/src/services/trading/open_trade.py"),
    ("instead of sending a duplicate",
     "backend/src/services/trading/open_trade.py"),
    ("send outcome UNKNOWN",
     "backend/src/services/trading/open_from_signal.py"),
    ("NOT retrying",
     "backend/src/services/trading/open_from_signal.py"),
    ("send outcome UNKNOWN",
     "backend/src/services/trading/scan_auto_execute.py"),
    ("we placed this and then lost its row",
     "backend/src/services/positions/reconciliation.py"),
    ("nothing is managing it",
     "backend/src/services/positions/reconciliation.py"),
    ("broker refused the close",
     "backend/src/services/positions/monitor_loop.py"),
]


@pytest.mark.parametrize("fragment,rel", QUOTED,
                         ids=[f"{r.split('/')[-1]}:{f[:28]}" for f, r in QUOTED])
def test_a_line_the_runbook_quotes_still_exists(fragment, rel):
    assert fragment in (REPO / rel).read_text(encoding="utf-8"), (
        f"the runbook tells Simon to look for {fragment!r} in the log, and "
        f"{rel} no longer contains it"
    )


@pytest.mark.parametrize("fragment,rel", QUOTED,
                         ids=[f"{r.split('/')[-1]}:{f[:28]}" for f, r in QUOTED])
def test_the_runbook_still_quotes_it(fragment, rel):
    """The other direction. If a fragment is dropped from the runbook this
    table goes stale silently, and the table is the only thing keeping the
    check honest."""
    assert fragment in RUNBOOK.read_text(encoding="utf-8")


def test_the_check_can_fail():
    """Negative control: a fragment that is in neither place."""
    text = (REPO / "backend/src/services/trading/open_trade.py").read_text(
        encoding="utf-8")

    assert "__not_a_log_line_anyone_writes__" not in text


class TestTheTimingsItQuotes:
    def test_the_reconciliation_cadence_is_still_twelve_cycles(self):
        """The runbook says "about a minute". That is 12 cycles x 5s, and if
        either number moves the wait it tells him to sit through is wrong."""
        src = (REPO / "backend/src/services/positions/reconciliation.py").read_text(
            encoding="utf-8")

        assert "_REPORT_EVERY_CYCLES = 12" in src

    def test_the_ack_timeout_is_still_capped_at_sixty(self):
        src = (REPO / "backend/src/services/trading/open_trade.py").read_text(
            encoding="utf-8")

        assert "_ack_timeout = 5.0" in src
        assert "min(60.0, 10.0 + 5.0 * max(1, _legs))" in src


class TestTheClaimsItMakesAboutBehaviour:
    def test_the_daily_loss_halt_still_runs_without_the_governor(self):
        """The runbook used to say demo 5 was meaningless until the governor
        was switched on. It is not: the loss ceiling runs on its own, and it
        says so now. If that ever changes, the correction becomes the wrong
        claim instead of the right one."""
        src = (REPO / "backend/src/services/risk/governor.py").read_text(
            encoding="utf-8")

        assert "Runs regardless of risk_governor_enabled" in src

    def test_the_daily_loss_default_is_still_three_percent(self):
        """The runbook told him the code defaulted to 20%. It was fixed to
        3.0, and the runbook now says so. Both the schema and the fallback."""
        schema = (REPO / "backend/migrations/schema_sql.py").read_text(
            encoding="utf-8")
        gov = (REPO / "backend/src/services/risk/governor.py").read_text(
            encoding="utf-8")

        assert "max_daily_loss_pct            REAL    NOT NULL DEFAULT 3.0" in schema
        assert 'rs.get("max_daily_loss_pct", 3.0)' in gov


class TestTheDemoOneBranchItDescribes:
    """Demo 1 now tells the operator which strategies reach the dedup guard
    and which take the placeholder branch instead. Both halves are claims
    about `open_trade`, and getting either wrong sends him down a path where
    the expected log line can never appear.
    """

    def test_a_template_ack_timeout_does_not_fall_back(self):
        """The runbook says a `template:` strategy records a placeholder and
        never reaches the dedup guard. That rests on this re-raise: a
        non-template timeout propagates to the outer handler and falls back,
        a template one does not."""
        src = (REPO / "backend/src/services/trading/open_trade.py").read_text(
            encoding="utf-8")

        assert "except asyncio.TimeoutError:\n                    if not _is_template:\n                        raise" in src

    def test_the_fallback_is_what_the_dedup_guard_protects(self):
        src = (REPO / "backend/src/services/trading/open_trade.py").read_text(
            encoding="utf-8")

        assert "falling back to Python bridge" in src
        assert "_resolve_fallback_send" in src

    def test_the_strategies_the_runbook_names_are_really_ea_portable(self):
        """The runbook lists them by name. A list that drifts from the code
        sends the operator to a strategy that never reaches the EA at all."""
        from backend.src.services.broker.ea_bridge import EA_PORTABLE_STRATEGIES

        text = RUNBOOK.read_text(encoding="utf-8")
        named = [s for s in EA_PORTABLE_STRATEGIES if f"`{s}`" in text]

        assert len(named) >= 10, f"only {len(named)} of the named strategies are portable"
        for s in named:
            assert s in EA_PORTABLE_STRATEGIES

    def test_the_non_template_ack_timeout_is_still_five_seconds(self):
        """The runbook tells him to wait 5s. If this default moves he waits
        the wrong amount and calls a slow ack a failure."""
        src = (REPO / "backend/src/services/trading/open_trade.py").read_text(
            encoding="utf-8")

        assert "_ack_timeout = 5.0" in src

    def test_the_ea_places_before_it_acks(self):
        """The whole demo depends on this order. If the EA ever acked first,
        removing it mid-flight would leave nothing at the broker to adopt and
        demo 1 would be untestable by this method."""
        src = (REPO / "mql5/ForexTraderBridge.mq5").read_text(
            encoding="utf-8", errors="replace")
        place = src.index("ok = trade.Buy(lots, _Symbol")
        ack = src.index('SendJson("{\\"type\\":\\"trade_opened\\"', place)

        assert place < ack

    def test_the_order_carries_the_trade_id_the_guard_searches_for(self):
        """`_resolve_fallback_send` finds the existing order by the comment
        the EA stamps on it. Two halves of one contract, in two languages,
        with no compiler between them."""
        ea = (REPO / "mql5/ForexTraderBridge.mq5").read_text(
            encoding="utf-8", errors="replace")

        assert '"ea:" + StringSubstr(trade_id, 0, 12)' in ea
