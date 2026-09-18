"""The Bounce engine's backend is deleted, and the Reversal Engine is untouched.

Owner, 2026-09-14: "remove the bounce engine but ensure you don't impact the
market context and anything else for the reversal engine as this is the main
signal generator ... while you can remove the bounce engine backend, don't
impact the reversal engine".

The panel went on 2026-09-02 and the engine was stopped on 2026-09-13
(`docs/todo/bugs/046`). This is the third and last step: the code goes too.

Deleting it is only safe because of what came out first. `services/test_signal/`
held eight functions that were never Bounce's -- the **Breakout** engine and
the **Reversal** engine both imported them, across a package boundary, because
that happened to be where they were first written. They now live in
`services/market/`, which is where a primitive every engine uses belongs:

  market/sessions.py    get_session, session_quality, session_is_active
  market/levels.py      compute_htf_bias, identify_key_levels, is_news_window
  market/indicators.py  compute_h4_bias, compute_adx, compute_macd_hist,
                        detect_regime
  market/macro_context.py  (was test_signal/market_context.py)
  market/news_window.py    (was test_signal/news_filter.py)

The last two are the ones the owner named. `reversal_engine/re_macro.py` and
`macro_backfill.py` read the macro context on every signal the live engine
scores, so the tests at the bottom check the Reversal Engine by exercising it,
not by checking that a file exists.

**The `bounce` slot in `_ENGINE_SERVICES` stays, bound to nothing.** It is
position 1 of 3 in a tuple the sync server and the mode toggle unpack
positionally, and `stood_down_engines` persists the name into the database.
Removing the slot would desynchronise a paired node that still has it; binding
it to a service whose `get_instance()` is always `None` costs one dict entry
and every call site already guards for `None`. See
`tests/frontend/test_bounce_generator_removed.py`, which still holds the
ordering.
"""
from __future__ import annotations

import importlib
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

_SCANNED = ("backend/src", "frontend")


# ── the engine is gone ───────────────────────────────────────────────────────

class TestThePackageIsDeleted:

    def test_the_package_directory_no_longer_exists(self):
        assert not (REPO / "backend/src/services/test_signal").exists()

    def test_it_cannot_be_imported(self):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("backend.src.services.test_signal")

    def test_its_tests_are_gone_too(self):
        """A test package for deleted code collects, fails on import and
        tells you nothing."""
        assert not (REPO / "tests/test_signal").exists()


class TestNothingStillImportsIt:
    """The failure this catches is a lazy `from ... import` inside a function
    body -- it survives every import-time check and raises the first time the
    branch runs, which for an engine path can be days later."""

    def _offenders(self) -> list[str]:
        bad = []
        for root in _SCANNED:
            for py in (REPO / root).rglob("*.py"):
                text = py.read_text(encoding="utf-8")
                for n, line in enumerate(text.splitlines(), 1):
                    code = line.split("#", 1)[0]
                    if "services.test_signal" in code:
                        bad.append(f"{py.relative_to(REPO)}:{n}: {line.strip()}")
        return bad

    def test_no_backend_or_frontend_module_imports_it(self):
        assert self._offenders() == []


# ── what had to survive the deletion ─────────────────────────────────────────

class TestTheSharedPrimitivesMovedRatherThanDied:
    """Not "the file exists" -- each one is called and its answer checked,
    because a re-export that lost its body would satisfy an import test."""

    def test_the_session_functions_answer(self):
        from backend.src.services.market import sessions as s
        assert s.get_session() in {
            "asian", "london", "overlap", "ny", "off", "closed"}
        assert s.session_quality("overlap") == "high"
        assert s.session_is_active("overlap") is True
        assert s.session_is_active("closed") is False

    def test_the_asian_grading_is_what_the_stored_parameter_gave(self):
        """`allow_asian` was 0.0 in the Bounce engine's store, so the Asian
        session graded "low". The store went with the engine; the behaviour
        did not change with it (bugs/045)."""
        from backend.src.services.market import sessions as s
        assert s.session_quality("asian") == "low"

    def test_the_level_functions_answer(self):
        from backend.src.services.market import levels as lv
        candles = [{"open": 2400 + i, "high": 2405 + i, "low": 2395 + i,
                    "close": 2402 + i, "time": i * 3600} for i in range(60)]
        assert lv.compute_htf_bias(candles) == "bullish"
        assert isinstance(lv.identify_key_levels(candles, 2430.0), list)
        assert isinstance(lv.is_news_window(), bool)

    def test_the_indicators_answer(self):
        from backend.src.services.market import indicators as ind
        candles = [{"open": 2400 + i, "high": 2405 + i, "low": 2395 + i,
                    "close": 2402 + i} for i in range(60)]
        assert ind.compute_h4_bias(candles) == "bullish"
        assert ind.compute_adx(candles) > 0
        assert len(ind.compute_macd_hist([2400.0 + i for i in range(60)])) == 2
        assert ind.detect_regime(30.0, "bullish", "bullish") == "trending"
        assert ind.detect_regime(10.0, "bullish", "bearish") == "ranging"

    def test_the_breakout_engine_still_reaches_every_one_of_them(self):
        """Breakout re-exports all ten off its own signal_generator. That
        module is what the live engine calls."""
        from backend.src.services.breakout_signal import signal_generator as bo
        for name in ("compute_htf_bias", "compute_h4_bias", "compute_adx",
                     "compute_macd_hist", "detect_regime", "identify_key_levels",
                     "get_session", "session_quality", "session_is_active",
                     "is_news_window"):
            assert callable(getattr(bo, name)), name


# ── the Reversal Engine, which the owner asked us not to break ───────────────

class TestTheReversalEngineIsUnaffected:

    def test_the_macro_context_it_reads_still_loads_and_answers(self, monkeypatch):
        """No network: `_latest` and the momentum helpers are the only things
        that reach for a wire, and they are stubbed. What is under test is the
        module surviving the move, not yfinance."""
        from backend.src.services.market import macro_context as mc
        monkeypatch.setattr(mc, "_latest", lambda symbol: 20.0)
        monkeypatch.setattr(mc, "_dxy_momentum", lambda: 0.01)
        monkeypatch.setattr(mc, "_tip_momentum", lambda: 0.01)
        assert isinstance(mc.get_context(), dict)

    def test_the_news_window_it_reads_still_answers(self):
        from backend.src.services.market import news_window as nw
        assert isinstance(nw.is_high_impact_window(), bool)

    def test_re_macro_still_builds_its_feature_block(self):
        """The live path: `re_macro` is called for every signal the engine
        scores, and it is the module the move actually touched."""
        from backend.src.services.reversal_engine import re_macro
        feats = re_macro.macro_features({}, None)
        assert len(feats) == len(re_macro.MACRO_FEATURE_NAMES)
        assert feats == [re_macro.MACRO_NEUTRAL[n]
                         for n in re_macro.MACRO_FEATURE_NAMES]

    def test_the_reversal_package_imports_nothing_from_the_dead_engine(self):
        pkg = REPO / "backend/src/services/reversal_engine"
        hits = [f"{p.relative_to(REPO)}"
                for p in pkg.rglob("*.py")
                if "services.test_signal" in p.read_text(encoding="utf-8")]
        assert hits == []


# ── the slot the sync protocol still expects ─────────────────────────────────

class TestTheBounceSlotIsEmptyRatherThanRemoved:

    def test_sub_engines_still_returns_three_in_the_binding_order(self):
        from backend.src.controllers import engines_controller as ec
        subs = ec.sub_engines()
        assert len(subs) == 3, (
            "api/routers/remote.py unpacks this as `breakout, bounce, reversal` "
            "and hands the three to server_start by keyword -- the arity is "
            "the contract")

    def test_the_bounce_slot_is_none(self):
        from backend.src.controllers import engines_controller as ec
        assert ec.sub_engines()[1] is None

    def test_engines_running_reports_it_as_not_running(self):
        from backend.src.controllers import engines_controller as ec
        assert ec.engines_running()["bounce"] is False

    def test_the_bulk_start_and_stop_survive_the_empty_slot(self):
        """Both iterate every slot. An AttributeError on the dead one would
        take the mode toggle down with it -- and the mode toggle is what hands
        trading between this node and the VPS.

        Read from the registry rather than the controller since 2026-09-18: the
        table and its two loops moved to services/engines/registry.py, where
        the handover can reach them without a service importing a controller.
        """
        from backend.src.services.engines import registry

        registry.start_stopped()
        registry.stop_running()


class TestAppStartupNoLongerStartsIt:

    def test_app_py_does_not_mention_the_dead_engine(self):
        code = (REPO / "backend/src/app.py").read_text(encoding="utf-8")
        assert "test_signal" not in code

    def test_the_watchdog_does_not_try_to_restart_it(self):
        """The app-level watchdog re-started any enabled-but-stopped engine
        every five minutes. Pointed at a deleted module it would log an
        exception on that schedule forever."""
        code = (REPO / "backend/src/app.py").read_text(encoding="utf-8")
        assert "sg_engine_enabled" not in code
