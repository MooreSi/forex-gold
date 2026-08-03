"""The Expert Tunables page reaches the catalogue through a controller (M7).

The settings page must not import the service directly: reads and writes
here hit the database, and every DB call from a NiceGUI callback has to go
through the controller layer's off-loop dispatch or it stalls the event
loop. That boundary is enforced by the frontend-never-imports-the-database
contract, which is one of the two enforced at zero.

These tests drive the controller functions the page calls, so the page
itself can stay a thin renderer over a catalogue it does not interpret.
"""
from __future__ import annotations

import pytest

from backend.src.controllers.settings import controller as ctl
from backend.src.services.risk import expert_params as ep


@pytest.fixture(autouse=True)
def _clean(fresh_db):
    ep.reset_all()
    yield
    ep.reset_all()


def test_the_catalogue_comes_back_grouped_for_rendering():
    """The page renders whatever it is handed, so grouping is the
    controller's job, not a set of hardcoded sections in the UI."""
    grouped = ctl.get_expert_param_catalogue()
    assert grouped, "no catalogue returned"
    assert "Risk filters" in grouped
    for domain, rows in grouped.items():
        assert rows, f"{domain} has no rows"
        for row in rows:
            # Plain dicts, not dataclasses -- the page must not need to
            # import the service to read a field off the object.
            assert isinstance(row, dict)
            for field in ("key", "label", "value", "default", "min", "max",
                          "unit", "desc", "integer"):
                assert field in row, f"{row.get('key')}: missing {field}"


def test_each_row_carries_the_live_value_and_its_default():
    """Both, deliberately: the page shows a reset control per row and has
    to know whether the current value differs from the default."""
    ep.set_params({"max_signal_age_s": 600})
    rows = {r["key"]: r for group in ctl.get_expert_param_catalogue().values()
            for r in group}
    assert rows["max_signal_age_s"]["value"] == 600
    assert rows["max_signal_age_s"]["default"] == 240
    assert rows["min_tp1_rr"]["value"] == rows["min_tp1_rr"]["default"] == 0.75


def test_saving_through_the_controller_clamps_like_the_service():
    """The clamp is a safety control; it must not live only in the UI,
    where a stale page or a direct call would bypass it."""
    ctl.save_expert_params({"min_tp1_rr": -1})
    assert ep.get("min_tp1_rr") == ep.spec("min_tp1_rr").min


def test_resetting_one_row_leaves_the_others_alone():
    ctl.save_expert_params({"max_signal_age_s": 600, "min_tp1_rr": 1.5})
    ctl.reset_expert_param("max_signal_age_s")
    assert ep.get("max_signal_age_s") == 240
    assert ep.get("min_tp1_rr") == 1.5


def test_resetting_everything_restores_every_default():
    ctl.save_expert_params({"max_signal_age_s": 600, "min_tp1_rr": 1.5})
    ctl.reset_all_expert_params()
    assert ep.get("max_signal_age_s") == 240
    assert ep.get("min_tp1_rr") == 0.75


def test_the_page_never_imports_the_service_directly():
    """Guards the boundary this controller exists to provide."""
    from pathlib import Path
    page = Path(__file__).resolve().parents[2] / "frontend" / "pages" / "settings.py"
    source = page.read_text()
    assert "risk.expert_params" not in source and "import expert_params" not in source, (
        "settings.py must reach Expert Tunables through the settings "
        "controller, not by importing the service"
    )
