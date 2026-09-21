"""The contradiction study is switched on, reachable, and off by default.

A study nothing calls records nothing and looks healthy doing it -- this
repo has shipped a guardrail that scanned a deleted directory and printed
"all good" for months. These tests assert the wiring itself, separately
from the behaviour, because the two fail independently.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.src.services.signals import contradiction_log as clog


ROOT = Path(__file__).resolve().parents[2]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestTheToggleExists:
    def test_a_migration_adds_the_column(self):
        src = _src("backend/migrations/steps_recent.py")
        assert "tg_contradiction_log_enabled" in src, \
            "the toggle has no migration, so every write fails on a real install"

    def test_it_defaults_to_off(self):
        """Rule 3: a new behaviour constant keeps the previous behaviour.
        Before this feature nothing was recorded and nothing reached the
        bus, so the default has to be 0."""
        src = _src("backend/migrations/steps_recent.py")
        line = next(l for l in src.splitlines()
                    if "tg_contradiction_log_enabled" in l)
        assert "DEFAULT 0" in line, line

    def test_it_is_editable_from_the_parsing_page(self):
        src = _src("frontend/src/components/parsing/content/settings.ts")
        assert "tg_contradiction_log_enabled" in src

    def test_it_travels_between_paired_nodes(self):
        from backend.src.services.cluster.sync.server import _SYNCED_SETTINGS_KEYS
        assert "tg_contradiction_log_enabled" in _SYNCED_SETTINGS_KEYS


class TestTheSchemaIsCreatedAtStartup:
    def test_app_startup_creates_it(self):
        """Creating the schema costs nothing while the toggle is off, and
        NOT creating it turns every write into a silent debug-level failure
        the day it is switched on."""
        src = _src("backend/src/app.py")
        assert "contradiction_log_repo" in src


class TestTheProducersCallIt:
    def test_the_telegram_scan_path_notes_every_new_signal(self):
        src = _src("backend/src/services/signals/scan_staleness.py")
        assert "note_signal" in src, \
            "Telegram signals never reach the bus, which is the whole point"

    def test_it_is_only_called_for_a_signal_that_is_genuinely_new(self):
        """A rescan must not re-observe. The UNIQUE constraint is the
        backstop; not calling it at all is the cheap part."""
        src = _src("backend/src/services/signals/scan_staleness.py")
        assert "_was_new" in src.split("note_signal")[0].rsplit("\n\n", 1)[-1] \
            or "if _is_new" in src

    def test_the_reversal_engine_observes(self):
        src = _src("backend/src/services/reversal_engine/reversal_engine_service.py")
        assert "contradiction_log" in src

    def test_the_breakout_engine_observes(self):
        src = _src("backend/src/services/breakout_signal/breakout_signal_service.py")
        assert "contradiction_log" in src


class TestItIsNotOnTheExecutionPath:
    """Stage 1 records and decides nothing. The resolver reaching a place
    that can place or refuse a trade is a separate change needing the owner
    and a demo session -- this is what would notice it arriving quietly."""

    FORBIDDEN = (
        "backend/src/services/signals/resolution.py",
        "backend/src/services/risk/governor.py",
        "backend/src/services/trading/open_from_signal.py",
        "backend/src/services/signals/pending_activation.py",
        "backend/src/services/signals/scan_auto_execute.py",
    )

    @pytest.mark.parametrize("rel", FORBIDDEN)
    def test_no_execution_gate_imports_the_resolver(self, rel):
        path = ROOT / rel
        if not path.exists():
            pytest.skip(f"{rel} does not exist")
        assert "contradiction" not in path.read_text(encoding="utf-8"), \
            f"{rel} reached for the contradiction resolver — that needs sign-off"

    def test_the_negative_control(self):
        """The check above passes trivially if the string never appears
        anywhere. Prove the same search finds it where it does belong."""
        src = _src("backend/src/services/signals/contradiction_log.py")
        assert "contradiction" in src


class TestTheReadout:
    def test_a_controller_exposes_the_report(self):
        from backend.src.controllers import telegram_controller as tg_ctl
        assert callable(tg_ctl.contradiction_report)

    def test_the_controller_only_forwards(self):
        """Controllers name an operation and forward it to one service."""
        body = inspect.getsource(
            __import__("backend.src.controllers.telegram_controller",
                       fromlist=["x"]).contradiction_report)
        assert "for " not in body and "if " not in body

    def test_an_endpoint_serves_it(self):
        src = _src("backend/src/api/routers/decision_log.py")
        assert "contradiction" in src


class TestTheDefaultsInCode:
    def test_the_setting_key_is_the_one_everything_else_names(self):
        assert clog.SETTING_KEY == "tg_contradiction_log_enabled"

    def test_the_telegram_bus_ttl_outlives_the_observation_window(self):
        """A row that expires inside the window a policy measures would make
        that policy look like it never fires."""
        assert clog.TELEGRAM_TTL_S > clog.OBSERVE_WINDOW_S
