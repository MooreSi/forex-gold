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
SOURCE = (REPO / "frontend/pages/settings/_capabilities.py").read_text(encoding="utf-8")

# The columns migration 41 added, read from the migration itself rather than
# restated here -- a list copied by hand is a list that drifts.
MIGRATION = (REPO / "backend/migrations/steps.py").read_text(encoding="utf-8")
STEP_41 = MIGRATION.split('(41, "Reversal-engine capability switches')[1].split("]),")[0]
COLUMNS = re.findall(r"ADD COLUMN (\w+)", STEP_41)


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
        risk = (REPO / "frontend/pages/settings/_risk.py").read_text(encoding="utf-8")
        assert "_render_capabilities_subcard" in risk

    def test_it_reaches_the_backend_through_the_controller(self):
        """frontend-reaches-the-backend-through-controllers is enforced at
        zero by the import-contract gate; asserted here too so the reason
        is visible where the code is."""
        assert "settings_ctl.update_risk_settings" in SOURCE
        assert "backend.src.services" not in SOURCE
