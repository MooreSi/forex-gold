"""The v9 label is computed on one lot size, not two.

docs/todo/reversal-engine/250, item 3. `_realised_r` divides
`net_pnl_dollars` by `sl_dist` at the 0.1-lot virtual rate. A virtual close
computes its dollars at 0.1 lot, so that is its R. An EXECUTED close stores
the broker's profit at the real lot (0.03 on demo) under the template's stop,
not `sl_dist` -- a full stop-out there reads about -0.3R. Executed rows are
left out of the label rather than rescaled: the row does not carry the lot
or the initial stop, and the trade the template ran is not the trade the
model's other rows describe.

Changes what the live ML gate learns after its next refit: owner sign-off
and a demo session before this merges.
"""
from __future__ import annotations

import pytest

from backend.src.services.reversal_engine import ml_engine as m


def test_a_broker_close_is_not_read_on_the_virtual_lot():
    # 0.03 lot stopped out 5 points away at the broker: -$15.
    row = {"sl_dist": 5.0, "net_pnl_dollars": -15.0, "live_exec_status": "executed"}

    assert m._realised_r(row) is None


@pytest.mark.parametrize("status", [None, "ml_skipped", "momentum_skipped",
                                    "filled_too_soon", "error:Max open trades reached (3)"])
def test_a_virtual_close_is_labelled_as_before(status):
    row = {"sl_dist": 5.0, "net_pnl_dollars": -50.0, "live_exec_status": status}

    assert m._realised_r(row) == pytest.approx(-1.0)
