"""Channel Strategy offers EA templates only.

Owner, 2026-09-03: "all channels are on templates, do 4 and 7".

Verified against the database before doing it, because the claim needed to be
true of the BINDINGS and not just of habit. Everything that traded today is on
a template:

    Reversal Engine             template:30 TP1 SL50 and Trail   131 trades
    Telegram Auto (GD INST)     template:GD Instituational        73
    Gold Diggers VIP            template:GD VIP - Single          69

But three bindings still name a built-in: Breakout Engine
(conservative_trial), Bounce Engine (conservative), and the fallback default
(scale_out) used by channels with no override -- which includes Manual Signal
and manual_market.

So this removes the built-ins from the PICKER and leaves every stored binding
untouched. Nothing changes about what an existing channel does; templates are
simply the only thing selectable from here on. Rewriting those three bindings
would change how live trades are managed and is the owner's call, not a
side-effect of tidying a dropdown.

The consequence that needed handling: a channel bound to `conservative_trial`
must still SHOW that, or the dropdown renders blank and the next save silently
rebinds it to whatever is first in the list.
"""
from __future__ import annotations

import pytest

from frontend.pages.trading import _strategy_cards as sc


class TestTheOptionsOffered:
    def test_templates_are_offered(self):
        opts = sc._strategy_options(
            templates=[{"name": "GD VIP - Single"}], current_values=[])

        assert any(v.startswith("Template: ") for v in opts.values())

    def test_no_builtin_strategy_is_offered(self):
        """The point of the change. STRATEGY_NAMES no longer seeds the list."""
        opts = sc._strategy_options(
            templates=[{"name": "GD VIP - Single"}], current_values=[])

        assert "Fixed R:R" not in opts.values()
        assert "Scale Out + Breakeven" not in opts.values()
        assert "Conservative Trial" not in opts.values()

    def test_inherit_and_auto_remain(self):
        """Inherit is how a channel says "use the default", and Auto is the
        AI pick -- both are still real answers."""
        opts = sc._strategy_options(templates=[], current_values=[])

        assert "" in opts
        assert "auto" in opts


class TestAnExistingBuiltinBindingStaysVisible:
    """Without this the dropdown renders blank for Breakout Engine, and the
    next save rebinds it to whatever happens to be first."""

    def test_the_current_value_is_kept_in_the_list(self):
        opts = sc._strategy_options(
            templates=[{"name": "GD VIP - Single"}],
            current_values=["conservative_trial"])

        assert "conservative_trial" in opts

    def test_it_is_labelled_so_it_reads_as_legacy(self):
        opts = sc._strategy_options(
            templates=[], current_values=["conservative_trial"])

        assert "Conservative Trial" in opts["conservative_trial"]
        assert "legacy" in opts["conservative_trial"].lower()

    def test_an_unknown_id_is_still_shown_rather_than_dropped(self):
        """A binding to a strategy that no longer exists must not vanish
        silently -- that is how a channel gets rebound by accident."""
        opts = sc._strategy_options(
            templates=[], current_values=["some_removed_strategy"])

        assert "some_removed_strategy" in opts

    def test_a_template_binding_needs_no_legacy_entry(self):
        opts = sc._strategy_options(
            templates=[{"name": "GD VIP - Single"}],
            current_values=["template:GD VIP - Single"])

        labels = [v for v in opts.values() if "legacy" in v.lower()]
        assert labels == []

    def test_blank_and_auto_do_not_become_legacy_entries(self):
        opts = sc._strategy_options(templates=[], current_values=["", "auto"])

        assert "legacy" not in opts[""].lower()
        assert "legacy" not in opts["auto"].lower()


class TestTheStrategyParametersCardIsGone:
    def test_the_page_no_longer_renders_it(self):
        import pathlib

        src = pathlib.Path(
            "frontend/pages/trading/_strategy.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

        assert "_render_strategy_params_card" not in code


class TestTheServiceBehindItSurvives:
    """Deleting the editor must not delete what it edited.

    `services/risk/strategy_params` is still live: `resolution.py` reads it for
    any channel whose stored binding is still a built-in, and Breakout Engine
    is on `conservative_trial` today. Only the UI and the controller
    pass-through that nothing else called are gone.
    """

    def test_the_strategy_params_service_still_exists(self):
        from backend.src.services.risk import strategy_params

        assert callable(strategy_params.get_strategy_params)

    def test_resolution_still_reads_it(self):
        import pathlib

        src = pathlib.Path(
            "backend/src/services/signals/resolution.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

        assert "get_strategy_params" in code

    def test_a_builtin_binding_still_resolves_its_parameters(self):
        """The behaviour that matters: Breakout Engine is bound to
        conservative_trial, and that binding must still produce parameters."""
        from backend.src.services.risk.strategy_params import get_strategy_params

        params = get_strategy_params("conservative_trial")

        assert isinstance(params, dict) and params

    def test_the_controller_passthrough_is_gone(self):
        """It had exactly one caller -- the card that was removed."""
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("backend.src.controllers.strategy_controller")
