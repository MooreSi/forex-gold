"""Executed broker dollars must not be divided by a virtual signal's risk."""
import pytest
from backend.src.services.breakout_signal import ml_labels as lb


@pytest.mark.parametrize("status", ["success", "executed"])
def test_broker_rows_are_excluded_without_broker_risk(status):
    assert lb.realised_r({"live_exec_status": status, "lot_size": .1,
                          "sl_dist": 5, "net_pnl_dollars": 100}) is None


def test_virtual_rows_retain_their_own_risk_denominator():
    assert lb.realised_r({"live_exec_status": "skipped:live_off", "lot_size": .1,
                          "sl_dist": 5, "net_pnl_dollars": 100}) == 2
