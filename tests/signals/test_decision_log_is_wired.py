"""The decision log is actually reachable, on every path that decides.

Four separate ways this feature could ship looking complete and record
nothing, so there is a test for each:

  * the column exists but no control writes it (the failure migration 41
    shipped with on 2026-09-11 -- fourteen switches nobody could reach);
  * the modules exist but no decision path calls them;
  * the sweep exists but no loop ticks it;
  * the table is never created, so every write fails quietly into a
    debug-level except.

Source-level where the wiring is what matters, behavioural where behaviour
is. Both, deliberately: `docs/todo/refactor` records a guardrail script that
scanned a deleted directory and printed "all good" for months.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]


def _statements(migrations) -> list[str]:
    """Every SQL string in the registry. A step may also be a callable --
    the data backfills -- and those are not iterable."""
    out = []
    for _number, _title, step in migrations:
        if isinstance(step, (list, tuple)):
            out.extend(s for s in step if isinstance(s, str))
    return out


class TestTheSettingExists:
    def test_a_migration_adds_the_column(self):
        from backend.migrations.steps import MIGRATIONS
        stmts = _statements(MIGRATIONS)
        assert any("tg_decision_log_enabled" in s for s in stmts), \
            "the toggle has no column, so nothing can persist it"

    def test_it_defaults_to_OFF(self):
        """It sits on the order path. An install that has not asked for it
        must not pay for it."""
        from backend.migrations.steps import MIGRATIONS
        stmt = next(s for s in _statements(MIGRATIONS)
                    if "tg_decision_log_enabled" in s)
        assert re.search(r"DEFAULT\s+0", stmt), stmt


class TestTheParsingPageCanReachIt:
    """The toggle's home moved from `frontend/pages/telegram/_keywords.py` to
    the React Parsing tab on 2026-09-18, in the same merge that brought the
    decision log across. The assertions are unchanged; only the file moved.

    `settings.ts` is the switch list as data — the same transcription-by-parsing
    the rest of the port used — so the wording checked below is the wording the
    operator reads.
    """

    SOURCE = (REPO / "frontend/src/components/parsing/content"
              / "settings.ts").read_text(encoding="utf-8")

    def test_the_toggle_is_on_the_parsing_page(self):
        assert "tg_decision_log_enabled" in self.SOURCE, \
            "the column cannot be turned on from the app"

    def test_the_toggle_is_actually_rendered(self):
        """A list nothing renders is the 2026-09-11 failure again: fourteen
        switches in the database whose only route to ON was editing it by
        hand. The section maps the list and binds each row both ways."""
        section = (REPO / "frontend/src/components/parsing/internal"
                   / "ParsingSettingsSection.tsx").read_text(encoding="utf-8")
        assert "PARSING_CATEGORIES" in section
        assert "onSave(" in section

    def test_it_says_it_changes_no_trading_decision(self):
        """The one thing an operator needs to know before switching it on."""
        lowered = self.SOURCE.lower()
        idx = lowered.index("tg_decision_log_enabled")
        blurb = lowered[idx:idx + 700]
        assert "records" in blurb or "recording" in blurb
        assert "no" in blurb and "decision" in blurb

    def test_the_page_reaches_the_backend_through_a_controller(self):
        """Under NiceGUI this meant "no service import". In the browser it is
        stronger: the tab cannot import Python at all, and the layer contract
        `the-api-layer-reaches-the-backend-through-controllers` holds the
        server side at zero. What is checked here is that the switch list is
        data and does not name a service module in a URL either."""
        assert "backend.src.services" not in self.SOURCE
        assert "backend/src/services" not in self.SOURCE


class TestTheDecisionPathsCallIt:
    def test_the_full_signal_path_records(self):
        src = (REPO / "backend/src/services/trading/scan_auto_execute.py").read_text(encoding="utf-8")
        assert "decision_log" in src

    def test_the_IME_path_records(self):
        """The path with no R:R filter and no opinion about the market is
        the one most in need of measuring."""
        src = (REPO / "backend/src/services/trading/instant_entry.py").read_text(encoding="utf-8")
        assert "decision_log" in src


class TestTheSweepIsTicked:
    def test_the_snapshot_cycle_runs_the_decision_sweep(self, monkeypatch):
        from backend.src.services.positions import core_signal_snapshot as snap
        seen = {"n": 0}

        async def _sweep(bridge):
            seen["n"] += 1

        state = snap.SnapshotState()
        asyncio.run(snap.run_snapshot_cycle(
            state, bridge=object(), now=1000.0,
            capture=_noop, background=_noop, resolve=_noop,
            decisions=_sweep))
        assert seen["n"] == 1

    def test_a_failing_decision_sweep_does_not_break_the_other_cadences(self, monkeypatch):
        from backend.src.services.positions import core_signal_snapshot as snap
        seen = {"capture": 0}

        async def _capture(bridge):
            seen["capture"] += 1

        async def _boom(bridge):
            raise RuntimeError("sweep exploded")

        asyncio.run(snap.run_snapshot_cycle(
            snap.SnapshotState(), bridge=object(), now=1000.0,
            capture=_capture, background=_noop, resolve=_noop,
            decisions=_boom))
        assert seen["capture"] == 1

    def test_it_is_paced_not_run_every_tick(self):
        """The sweep reads the trade ledger and may call the bridge. At the
        5s capture cadence that is a needless read every five seconds."""
        from backend.src.services.positions import core_signal_snapshot as snap
        assert snap.DECISION_SWEEP_INTERVAL_S >= 30.0


class TestTheTableIsCreatedAtStartup:
    def test_startup_creates_the_schema(self):
        src = (REPO / "backend/src/app.py").read_text(encoding="utf-8")
        assert "decision_log_repo" in src, (
            "without this every write fails into a debug-level except and "
            "the feature is silently a no-op")


async def _noop(bridge):
    return 0


class TestTheDefaultSweepIsTheOneThatRunsLive:
    """runtime.py passes `capture` and `background` and nothing else, so the
    live loop reaches `_sweep_decision_log` by default. The injected seam
    above is what the cadence tests drive; this is the function that
    actually runs, and without it the feature records and never resolves."""

    def test_the_live_loop_does_not_inject_a_decision_sweep(self):
        src = (REPO / "backend/src/runtime.py").read_text(encoding="utf-8")
        call = src[src.index("await _snap.run_snapshot_cycle("):][:400]
        assert "decisions=" not in call, (
            "runtime injects its own sweep; this test is checking the wrong "
            "function")

    def test_it_resolves_outcomes_before_scoring_the_shadow(self):
        """Order matters: the report joins on a resolved outcome, so a trade
        that closed in this window is scoreable in the same pass."""
        import asyncio
        from backend.src.services.positions import core_signal_snapshot as snap
        from backend.src.services.signals import decision_outcomes, decision_shadow

        order = []

        def _resolve(*a, **k):
            order.append("outcomes")
            return 0

        async def _evaluate(bridge, *a, **k):
            order.append("shadow")
            return 0

        with mock.patch.object(decision_outcomes, "resolve_pending", _resolve), \
             mock.patch.object(decision_shadow, "evaluate_pending", _evaluate):
            asyncio.run(snap._sweep_decision_log(bridge=object()))

        assert order == ["outcomes", "shadow"]

    def test_it_runs_the_ledger_read_off_the_event_loop(self):
        """resolve_pending is synchronous SQLite against the core db. Called
        directly it would block the loop that is also running the monitor."""
        src = (REPO / "backend/src/services/positions/core_signal_snapshot.py").read_text(
            encoding="utf-8")
        body = src[src.index("async def _sweep_decision_log"):]
        assert "to_db_thread" in body[:900]


class TestTheReadoutIsReachable:
    """A log nobody can read is the failure migration 41 shipped with on
    2026-09-11 -- fourteen capability switches whose only route to ON was
    editing the trading database by hand. The switch went in first here
    too, so this is the test that says the readout followed it."""

    # The readout moved to the React Parsing tab on 2026-09-18. The three
    # controller assertions are unchanged; the page ones now read the React
    # component, and the "reachable" one reads the panel that renders it.
    #
    # ROUTER is new and is the piece the NiceGUI version did not need: in the
    # browser the component cannot call a controller, so the endpoints are
    # what make the readout reachable at all. A component that named
    # `decision_log_summary` with no route behind it would satisfy the old
    # assertions and show nothing.
    SOURCE = (REPO / "frontend/src/components/parsing/internal"
              / "DecisionLogSection.tsx").read_text(encoding="utf-8")
    PANEL = (REPO / "frontend/src/components/parsing"
             / "ParsingPanel.tsx").read_text(encoding="utf-8")
    ROUTER = (REPO / "backend/src/api/routers"
              / "decision_log.py").read_text(encoding="utf-8")
    CONTROLLER = (REPO / "backend/src/controllers/telegram_controller.py").read_text(
        encoding="utf-8")

    def test_the_controller_exposes_the_summary(self):
        assert "def decision_log_summary" in self.CONTROLLER

    def test_the_controller_exposes_the_variant_report(self):
        assert "def decision_log_report" in self.CONTROLLER

    def test_the_controller_exposes_the_backfill(self):
        assert "def decision_log_backfill" in self.CONTROLLER

    def test_the_page_renders_the_summary(self):
        assert "decision_log_summary" in self.ROUTER
        assert "/api/decision-log/summary" in self.SOURCE

    def test_the_page_renders_the_variant_report(self):
        assert "decision_log_report" in self.ROUTER
        assert "/api/decision-log/report" in self.SOURCE

    def test_the_page_offers_the_backfill(self):
        assert "decision_log_backfill" in self.ROUTER
        assert "/api/decision-log/backfill" in self.SOURCE

    def test_the_page_still_reaches_the_backend_only_through_the_controller(self):
        """The router's only backend import is the controller. This is the
        same claim the old page made about itself, moved to the layer that now
        makes it — and it is enforced at zero by the import contracts as well,
        so this is the readable statement of a machine-checked rule."""
        assert "backend.src.services" not in self.ROUTER
        assert "from backend.src.controllers import telegram_controller" in self.ROUTER

    def test_it_is_rendered_somewhere_a_user_can_reach(self):
        """A component nothing renders renders nothing."""
        assert "DecisionLogSection" in self.PANEL
        assert "<DecisionLogSection" in self.PANEL

    def test_the_router_is_mounted(self):
        """...and a router nothing mounts answers nothing. `server.py` is the
        composition root; a file that is never in ROUTERS is a 404."""
        server = (REPO / "backend/src/api/server.py").read_text(encoding="utf-8")
        assert "decision_log_router.router" in server

    def test_a_variant_with_no_evidence_is_not_rendered_as_zero(self):
        """`report()` returns mean_r None, never 0.0, precisely so this can
        say so. Formatting it with toFixed(3) would put 0.000 next to a
        variant that has never scored anything."""
        assert "no decisions yet" in self.SOURCE
        assert "=== null" in self.SOURCE


class TestTheToggleTravelsBetweenNodes:
    def test_it_is_in_the_synced_settings_list(self):
        """A per-node research toggle would mean the Mac and the VPS record
        different halves of the same study and neither says so. Its
        neighbours on the Parsing page -- accept_tg_signals,
        auto_execute_signals, exclude_high_risk -- are all synced."""
        from backend.src.services.cluster.sync.server import _SYNCED_SETTINGS_KEYS
        assert "tg_decision_log_enabled" in _SYNCED_SETTINGS_KEYS
