"""One credit per realised cashflow, including partials. Temporary SQLite only."""
import pytest
from backend.src.services.breakout_signal import breakout_signal_repo as repo


def test_partial_then_final_credits_only_the_unbooked_remainder(tmp_path):
    repo.init(str(tmp_path / "bo.db"))
    try:
        sid = repo.create_signal({"signal_ref": "regression", "direction": "BUY", "breakout_type": "go",
                                  "entry_mid": 100, "stop_loss": 95, "lot_size": .1})
        repo.book_partial_close(sid, 10, .33, "partial")
        repo.close_signal(sid, 100, "win", net_pnl_dollars=15, net_pnl_pts=1.5)
        assert repo.get_virtual_balance() == pytest.approx(1015)
        repo.close_db()
        repo.init(str(tmp_path / "bo.db"))
        repo.close_signal(sid, 100, "win", net_pnl_dollars=15, net_pnl_pts=1.5)
        assert repo.get_virtual_balance() == pytest.approx(1015)
        assert repo.reconcile_balance_with_trades() is None
    finally:
        repo.close_db()
