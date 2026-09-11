"""Every capability switch has a control, and the control writes it back.

Migration 41 added fourteen columns to `vantage_risk_settings`, one per
capability from docs/todo/reversal-engine/200. They shipped on 2026-09-11
with no UI at all, so the only way to turn any of them on was to edit the
trading database by hand. A switch nobody can reach is not a switch, and
this is the check that says so.

Source-level, like the other frontend wiring tests: what breaks here is a
column added to the migration and forgotten in the form, and that is
exactly what reading the source catches.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOURCE = (REPO / "frontend/pages/reversal_panel/_capabilities.py").read_text(encoding="utf-8")

# The columns migration 41 added, read from the migration LIST rather than
# from a source file -- a list copied by hand is a list that drifts, and a
# file parsed by hand breaks the moment the file is split, which is exactly
# what happened to the first version of this on 2026-09-11 when steps.py hit
# its line ceiling and migrations 39 onward moved to steps_recent.py.
from backend.migrations.steps import MIGRATIONS  # noqa: E402

_STEP_41 = next(step for number, _t, step in MIGRATIONS if number == 41)
COLUMNS = [m.group(1) for stmt in _STEP_41
           for m in [re.search(r"ADD COLUMN (\w+)", stmt)] if m]


def test_the_migration_actually_added_fourteen_columns():
    """Guards the guard: if the parse above silently found nothing, every
    other test in this file would pass vacuously."""
    assert len(COLUMNS) == 14, COLUMNS


@pytest.mark.parametrize("column", COLUMNS)
def test_the_column_is_read_into_a_control(column):
    assert f'rs.get("{column}"' in SOURCE, (
        f"{column} has no control: it cannot be turned on from the app")


@pytest.mark.parametrize("column", COLUMNS)
def test_the_column_is_written_back_on_save(column):
    assert f'"{column}":' in SOURCE, (
        f"{column} is displayed but never saved")


class TestItIsHonestAboutWhatTheseDo:
    def test_the_card_says_a_demo_session_is_needed(self):
        assert "demo" in SOURCE.lower()

    def test_it_is_rendered_somewhere_a_user_can_reach(self):
        """Signal Generator > Reversal Engine since 2026-09-11. It used to
        sit on the Trading page, a long way from the panel that shows
        whether any of it is working."""
        panel = (REPO / "frontend/pages/reversal_panel/__init__.py").read_text(
            encoding="utf-8")
        assert "render_capabilities_subcard" in panel

    def test_it_reaches_the_backend_through_the_controller(self):
        """frontend-reaches-the-backend-through-controllers is enforced at
        zero by the import-contract gate; asserted here too so the reason
        is visible where the code is."""
        assert "settings_ctl.update_risk_settings" in SOURCE
        assert "backend.src.services" not in SOURCE


class TestTheAiControls:
    """Owner request 2026-09-11: a Recommend button that uses the ML
    evidence and the AI together, and an AI switch that hands the settings
    over permanently."""

    def test_recommend_is_offered_and_writes_nothing_by_itself(self):
        assert "reversal_ai_recommend" in SOURCE
        # Apply is a separate, deliberate press.
        assert "reversal_ai_apply" in SOURCE
        assert SOURCE.index("reversal_ai_recommend") < SOURCE.index("reversal_ai_apply")

    def test_the_ai_switch_is_wired_to_its_own_column(self):
        assert 're_ai_tuning_enabled' in SOURCE

    def test_the_ai_switch_warns_that_it_acts_without_confirmation(self):
        """It changes live trading settings every fifteen minutes with
        nobody watching. The tooltip has to say so."""
        lowered = SOURCE.lower()
        assert "15 minutes" in lowered
        assert "real money" in lowered

    def test_the_card_says_what_the_ai_cannot_touch(self):
        lowered = SOURCE.lower()
        assert "sizing" in lowered and "live execution" in lowered

    def test_the_page_reaches_the_ai_through_the_controller(self):
        assert "engines_ctl." in SOURCE
        assert "backend.src.services" not in SOURCE
