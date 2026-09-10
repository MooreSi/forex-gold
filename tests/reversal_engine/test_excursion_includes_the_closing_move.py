"""The excursion watermark must include the move that ended the trade.

`_record_live_excursion` samples once per five-second outcome loop, and
`_reconcile_live_signal` says so itself: *"this is the ONLY moment the live
path sees a running price -- so it is where the excursion has to be taken."*

So a move that reaches the stop and closes the position between two samples is
never recorded. `mae_pts` is biased **downwards for fast moves**, which are
exactly the moves that hit stops.

That matters because [reversal-engine/020](../../docs/todo/reversal-engine/020-losses-exceed-the-stop.md)
plans to compare `mae_pts` against `sl_dist` to decide whether stops are being
honoured — and with this bias a correctly honoured stop reads `mae < sl_dist`,
which that comparison would call "the stop was never reached". The first nine
losses carrying both figures had a mean ratio of 0.821 and a median of 0.686.

The reconcile path already computes the real exit from the broker's own closing
deals (`close_price`, and `pnl_pts` measured from the same `entry_ref` the
sampler uses). Recording one final excursion there costs nothing and closes the
gap between the last sample and the exit.

**Recording only.** It moves no stop and closes nothing, exactly as
`_record_live_excursion` promises of itself.
"""
from __future__ import annotations

import inspect

import pytest

from backend.src.services.reversal_engine import reversal_engine_manage as mng
from backend.src.services.reversal_engine import reversal_engine_repo as repo


@pytest.fixture
def sig(tmp_path):
    """A live RE database with one signal, following test_schema_extraction's
    setup (repo.init on a tmp file)."""
    repo.init(str(tmp_path / "reversal.db"))
    repo.get_db().run(
        "INSERT INTO re_signals (signal_ref, direction, entry_low, entry_high, "
        "stop_loss, sl_dist, created_at) VALUES (?,?,?,?,?,?,?)",
        "RE-TEST", "BUY", 4400.0, 4402.0, 4395.0, 7.0, 1788000000.0,
    )
    row = repo.get_db().get("SELECT id FROM re_signals WHERE signal_ref='RE-TEST'")
    return int(row["id"])


def _mae(sid):
    r = repo.get_db().get("SELECT mfe_pts, mae_pts FROM re_signals WHERE id=?", sid)
    return (r["mfe_pts"], r["mae_pts"])


class TestTheClosingMoveIsRecorded:
    @staticmethod
    def _body() -> str:
        src = inspect.getsource(mng.ReversalEngineManageMixin._reconcile_live_signal) \
            if hasattr(mng, "ReversalEngineManageMixin") else inspect.getsource(mng)
        return "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith("#"))

    def test_the_reconcile_path_records_a_final_excursion(self):
        body = self._body()

        assert body.count("record_excursion(") >= 2, (
            "only the 5s sampler records an excursion; the closing move, which "
            "is the one that hits the stop, is still never captured"
        )

    def test_it_uses_the_broker_exit_not_the_last_tick(self):
        """The whole point is the price the trade actually ended at.

        Asserted on the CALL LINE itself. A window of surrounding lines let a
        mutant recording `record_excursion(sig_id, 0.0, 0.0)` survive, because
        the `close_signal(...)` call two lines below also mentions `pnl_pts`.
        """
        lines = [l for l in self._body().splitlines() if "record_excursion(" in l]

        assert lines, "no record_excursion call in the reconcile path"
        assert any("pnl_pts" in l for l in lines), (
            f"the final excursion is not measured from the broker's close: "
            f"{[l.strip() for l in lines]}"
        )


class TestTheWatermarkStillOnlyWidens:
    """`record_excursion` is MAX-based in SQL. A closing move smaller than an
    excursion already seen must not shrink it — otherwise adding this call
    would DESTROY the data 020 is waiting for."""

    def test_a_smaller_closing_move_does_not_narrow_mae(self, sig):
        repo.record_excursion(sig, 0.0, 9.0)     # a deep adverse move
        repo.record_excursion(sig, 0.0, 2.0)     # a shallower close

        assert _mae(sig)[1] == 9.0

    def test_a_larger_closing_move_widens_it(self, sig):
        repo.record_excursion(sig, 0.0, 2.0)
        repo.record_excursion(sig, 0.0, 9.0)

        assert _mae(sig)[1] == 9.0

    def test_a_winning_close_records_no_adverse_excursion(self, sig):
        """A win passes a NEGATIVE adverse figure; it is clamped to 0 and must
        not appear as an adverse move."""
        repo.record_excursion(sig, 5.0, -5.0)

        assert _mae(sig) == (5.0, 0.0)
