"""bugs/060: the owner turned the dual-bias gate on (2026-10-11).

`require_dual_bias` defaulted to 0.0 after 2026-09-16 because the gate had
never once rejected a signal (H4 bias was starved to "neutral") and repairing
the feed should not arm it silently. The owner has now decided: on.

The decision is applied as a stored value, once per database, rather than a
new code default. The stored value is what `get()` reads, and every node's
`bo_config` already holds 0.0, so a default change alone would do nothing.
`tests/breakout_signal/test_h4_bias_is_actually_computed.py` still pins the
default at 0.0 and is unmodified.

The param is tuner-locked so the nightly AI analysis cannot switch it back
off (the prompt allows it to set this "exactly 0 or 1").

Nothing here reaches a broker.
"""
import os
import tempfile

import pytest

from backend.src.services.breakout_signal import adaptive_params as ap
from backend.src.services.breakout_signal import breakout_signal_repo as repo
from tests.conftest import remove_db_file


@pytest.fixture
def bo_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    repo.close_db()
    remove_db_file(path)


def test_a_database_holding_off_is_switched_on(bo_db):
    repo.set_config("bo_ap:require_dual_bias", "0.0")

    ap.apply_owner_decisions()

    assert ap.get("require_dual_bias") == 1.0


def test_it_applies_once_so_a_later_choice_is_respected(bo_db):
    """Once applied, a deliberate later change (a code or UI decision) is not
    overwritten on the next start."""
    ap.apply_owner_decisions()
    repo.set_config("bo_ap:require_dual_bias", "0.0")

    ap.apply_owner_decisions()

    assert ap.get("require_dual_bias") == 0.0


def test_the_ai_tuner_cannot_switch_it_off(bo_db):
    ap.apply_owner_decisions()

    assert ap.apply_adjustment("require_dual_bias", 0.0, "tuner says so") is None
    assert ap.get("require_dual_bias") == 1.0
