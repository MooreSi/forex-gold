"""A template's fixed Anchor Lot is never scaled by the channel multiplier.

Owner, 2026-09-03: "why did ticket 1925815819 open with 0.13 lots? it does
this occasionally as opposed to the set 0.1".

Because `_lot_is_template_fixed` was set only INSIDE
`if not lot_size and _is_template ...`. A signal carrying its own lot skipped
that branch, left the flag False, and the channel multiplier then scaled a
deliberately fixed lot:

    template GD VIP - Single   lot_anchor 0.10, risk_pct 0.0
    Telegram Auto (Gold Diggers VIP)   lot_mult 1.3
    0.10 x 1.3 = 0.13

The Telegram Auto route stores a lot on the signal; plain-channel signals do
not, take the branch, and were correct. That is why it looked occasional --
76 trades at 0.10 against 5 at 0.13 in the same week.

The flag is now derived from the TEMPLATE, so it holds however the lot was
obtained. `risk_pct > 0` templates are deliberately still scaled: that path is
genuinely risk-derived, the same as generic sizing, and the multiplier is
meant to apply to it.

MONEY PATH. These tests use the real decision code with fakes -- no broker, no
order. The change still wants a demo before it is trusted live.
"""
from __future__ import annotations

import pytest


def _flag(is_template: bool, template: dict | None) -> bool:
    """The decision under test, as resolution.py now makes it."""
    from backend.src.services.signals import resolution as res

    return res._template_lot_is_fixed(is_template, template)


class TestAFixedAnchorTemplate:
    def test_is_exempt_from_the_multiplier(self):
        assert _flag(True, {"lot_anchor": 0.10, "risk_pct": 0.0}) is True

    def test_is_exempt_even_when_risk_pct_is_missing(self):
        """An older template row has no risk_pct column value at all."""
        assert _flag(True, {"lot_anchor": 0.10}) is True

    def test_is_exempt_when_risk_pct_is_none(self):
        assert _flag(True, {"lot_anchor": 0.10, "risk_pct": None}) is True

    def test_is_exempt_when_risk_pct_is_an_empty_string(self):
        """SQLite hands back '' for an unset numeric column. `'' or 0` is 0,
        so this takes the normal path rather than the error one."""
        assert _flag(True, {"lot_anchor": 0.10, "risk_pct": ""}) is True

    def test_an_unreadable_risk_pct_stays_exempt(self):
        """A value that genuinely raises -- not '' , which short-circuits to 0
        and never reaches the handler. Mutation testing caught that the error
        branch was untested.

        Fail SAFE, not open: an unreadable risk_pct must not silently turn a
        deliberately fixed lot into a scaled one. Wrong here means trading 30%
        larger than the owner set.
        """
        assert _flag(True, {"lot_anchor": 0.10, "risk_pct": "not a number"}) is True


class TestARiskBasedTemplate:
    def test_is_still_scaled(self):
        """Deliberate. That path derives the lot from account risk, the same
        as generic sizing, so the channel multiplier is meant to apply."""
        assert _flag(True, {"lot_anchor": 0.10, "risk_pct": 0.5}) is False


class TestNonTemplates:
    def test_a_plain_strategy_is_not_exempt(self):
        assert _flag(False, None) is False

    def test_a_template_flag_with_no_template_is_not_exempt(self):
        """Belt and braces: is_template true but the row failed to load."""
        assert _flag(True, None) is False


class TestTheCallSiteUsesIt:
    """The helper being right is worth nothing if resolution.py does not ask
    it. Mutation testing replaced the call with `False` and every behavioural
    test above still passed, because they all call the helper directly."""

    def test_resolution_derives_the_flag_from_the_helper(self):
        import pathlib as _pl

        src = _pl.Path(
            "backend/src/services/signals/resolution.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                          if not ln.strip().startswith("#"))

        assert "_lot_is_template_fixed = _template_lot_is_fixed(" in code

    def test_it_is_not_hardcoded_anywhere(self):
        """A stray `_lot_is_template_fixed = True/False` would override the
        helper depending on order."""
        import pathlib as _pl

        src = _pl.Path(
            "backend/src/services/signals/resolution.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                          if not ln.strip().startswith("#"))

        assert "_lot_is_template_fixed = True" not in code
        assert "_lot_is_template_fixed = False" not in code

    def test_the_multiplier_still_checks_the_flag(self):
        """The exemption only means anything at the point the multiplier is
        applied."""
        import pathlib as _pl

        src = _pl.Path(
            "backend/src/services/signals/resolution.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                          if not ln.strip().startswith("#"))

        assert "not _lot_is_template_fixed" in code


class TestTheReportedTrade:
    """The exact numbers from ticket 1925815819."""

    def test_it_would_now_stay_at_the_anchor_lot(self):
        template = {"lot_anchor": 0.10, "risk_pct": 0.0}
        lot, mult = 0.10, 1.3

        if not _flag(True, template):
            lot = lot * mult

        assert round(lot, 2) == 0.10

    def test_and_the_bug_is_reproducible_without_the_flag(self):
        """Negative control: 0.13 is what the old behaviour produced, so this
        test would have passed before the fix if the assertion were loose."""
        lot, mult = 0.10, 1.3

        assert round(lot * mult, 2) == 0.13
