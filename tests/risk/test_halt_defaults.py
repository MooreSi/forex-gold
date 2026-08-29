"""Protective halt defaults and their fallbacks (stage3/050).

Every protective halt ships OFF (review risk M1), so always-on protection is
effectively `max_open_trades` and the lot cap. Simon confirmed the numbers on
2026-08-25 in docs/simon-handover/001-trading-defaults.md: **3% daily loss,
10% drawdown from peak, 3 losing trades in a row**, and halts pause new opens
only -- they never close anything.

The sharper problem is not the switch, it is that the same limit is written
down three times with three different numbers. The schema says one thing, the
Settings screen says another, and `governor.py` -- the code that actually
enforces it -- falls back to a third. The enforcement path had the LOOSEST
value, so a missing key meant a 20% daily loss limit rather than 3%.

These tests pin the numbers to one source, and pin the halts as
pause-new-opens-only.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import governor


# The numbers Simon confirmed. Written as literals on purpose: a test that
# reads them from the same place the code does cannot detect a drift.
CONFIRMED_DAILY_LOSS_PCT = 3.0
CONFIRMED_DRAWDOWN_PCT = 10.0
CONFIRMED_CONSEC_LOSSES = 3


class TestTheFallbacksMatchTheSchema:
    """A fallback is what runs when the key is missing. If it is looser than
    the configured default, a missing key silently widens the limit."""

    def test_the_daily_loss_fallback_is_not_looser_than_the_default(self):
        """It was 20.0 against a schema default of 3.0 -- an absent key gave
        six times the intended loss allowance, in the one function that
        decides whether trading stops."""
        import inspect
        src = inspect.getsource(governor)

        assert 'rs.get("max_daily_loss_pct", 20.0)' not in src, (
            "the daily-loss enforcement path falls back to 20% while the "
            "schema default is 3%")

    def test_the_drawdown_fallback_is_not_looser_than_the_default(self):
        import inspect
        src = inspect.getsource(governor)

        assert 'rs.get("max_total_drawdown_pct", 20.0)' not in src, (
            "the drawdown enforcement path falls back to 20% while the "
            "schema default is 10%")

    def test_a_missing_daily_loss_key_HALTS_at_the_confirmed_number(
            self, fresh_db, monkeypatch):
        """Behavioural, not textual. With the key absent and the day down 4%,
        a 3% limit halts and a 20% limit does not -- so this test tells the
        two fallbacks apart rather than reading the source."""
        from backend.src.services.risk import repo as risk_repo
        monkeypatch.setattr(risk_repo, "sum_realised_pnl_since", lambda _ts: -40.0)

        reason = governor.rg_check_halt({}, balance=960.0)

        assert reason is not None, (
            "a 4% daily loss did not halt — the fallback is looser than the "
            "3% Simon confirmed")
        assert "aily loss" in reason

    def test_a_missing_key_does_NOT_halt_inside_the_limit(self, fresh_db,
                                                          monkeypatch):
        """The negative control. Down 2% must keep trading, or the test above
        would pass with a limit of zero, which halts on any loss at all."""
        from backend.src.services.risk import repo as risk_repo
        monkeypatch.setattr(risk_repo, "sum_realised_pnl_since", lambda _ts: -20.0)

        assert governor.rg_check_halt({}, balance=980.0) is None


class TestTheSchemaDefaultsMatchWhatSimonConfirmed:
    """The schema is what a fresh install gets."""

    @staticmethod
    def _schema() -> str:
        import io
        return io.open("backend/migrations/schema_sql.py", encoding="utf-8").read()

    def test_daily_loss_defaults_to_three_percent(self):
        assert "max_daily_loss_pct            REAL    NOT NULL DEFAULT 3.0" in self._schema()

    def test_drawdown_defaults_to_ten_percent(self):
        """Was 8.0. Simon confirmed 10%."""
        assert "max_total_drawdown_pct        REAL    NOT NULL DEFAULT 10.0" in self._schema()

    def test_max_open_trades_is_untouched(self):
        """Explicitly out of scope for this task, and a limit in its own
        right -- widening it here would be a silent risk increase."""
        assert "max_open_trades               INTEGER NOT NULL DEFAULT 1" in self._schema()


class TestHaltsPauseOpensAndNothingElse:
    def test_the_governor_module_closes_nothing(self):
        """Halt semantics are pause-new-opens-only. A halt that closed
        positions would turn a protective limit into a forced liquidation at
        whatever price happens to be showing."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(governor))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name):
                    called.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    called.add(fn.attr)

        forbidden = {"close_position", "close_trade", "record_close",
                     "partial_close_trade", "close_all_ladder_legs"}
        assert not (called & forbidden), (
            f"the risk governor calls {called & forbidden} — halts must pause "
            "new opens, never close a position")
