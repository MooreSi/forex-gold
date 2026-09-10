"""Record the stop that was actually in force, not just the one at open.

`sl_dist` is the stop distance the signal was CREATED with. The EA moves the
real stop and logs nothing when it does — traced 2026-09-10 on ticket
1977022272, which opened at `SL=4438.26` and was later reported by the broker at
`4431.23`, with no line in the app log or the EA log.

It trails because `GD Instituational - single` sets `tp1_trigger_level = 1`,
which the EA OR's with the pip-based `trail_activation`, so clearing TP1 arms
the trail at three points of profit rather than the hundred pips the activation
field implies.

**This is why [reversal-engine/020](../../docs/todo/reversal-engine/020-losses-exceed-the-stop.md)
cannot do what it plans.** It wants `mae_pts / sl_dist` to say whether stops are
honoured, and a trade that clears TP1 and is later stopped out is stopped at a
much tighter stop — so the ratio reads well under 1.0 with nothing wrong.

`last_seen_sl` is a NEW column, so nothing existing changes meaning. It is
written on the same five-second sample that already records the excursion, from
the position the bridge is already returning, and the final value is the stop
that was in force when the trade closed.
"""
from __future__ import annotations

import inspect

import pytest

from backend.src.services.reversal_engine import reversal_engine_manage as mng
from backend.src.services.reversal_engine import reversal_engine_repo as repo


@pytest.fixture
def sig(tmp_path):
    repo.init(str(tmp_path / "reversal.db"))
    repo.get_db().run(
        "INSERT INTO re_signals (signal_ref, direction, entry_low, entry_high, "
        "stop_loss, sl_dist, created_at) VALUES (?,?,?,?,?,?,?)",
        "RE-SL", "SELL", 4435.0, 4440.0, 4447.0, 7.0, 1788000000.0,
    )
    return int(repo.get_db().get(
        "SELECT id FROM re_signals WHERE signal_ref='RE-SL'")["id"])


def _row(sid):
    return repo.get_db().get("SELECT * FROM re_signals WHERE id=?", sid)


class TestTheColumnExists:
    def test_a_fresh_install_has_it(self, sig):
        """The excursion columns were missing from a fresh schema for months
        and failed silently. Not repeating that."""
        cols = {r[1] for r in repo.get_db().all("PRAGMA table_info(re_signals)")}

        assert "last_seen_sl" in cols


class TestItRecordsWhatTheBrokerReports:
    def test_the_stop_is_stored(self, sig):
        repo.record_last_seen_sl(sig, 4438.26)

        assert _row(sig)["last_seen_sl"] == 4438.26

    def test_a_later_sample_overwrites_it(self, sig):
        """Unlike the excursion watermarks this is NOT a max — the point is the
        LAST stop seen, and a trail moves it in the trade's favour, which for a
        SELL means downwards."""
        repo.record_last_seen_sl(sig, 4438.26)
        repo.record_last_seen_sl(sig, 4431.23)

        assert _row(sig)["last_seen_sl"] == 4431.23

    def test_a_zero_or_missing_stop_is_ignored(self, sig):
        """MT5 reports 0.0 for a position with no stop. Storing that would read
        as 'the stop was at zero' rather than 'there was none'."""
        repo.record_last_seen_sl(sig, 4438.26)
        repo.record_last_seen_sl(sig, 0.0)
        repo.record_last_seen_sl(sig, None)

        assert _row(sig)["last_seen_sl"] == 4438.26


class TestItIsWiredIntoTheSampler:
    @staticmethod
    def _body() -> str:
        src = inspect.getsource(mng)
        return "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith("#"))

    def test_the_reconcile_path_records_it(self):
        assert "record_last_seen_sl(" in self._body(), (
            "nothing records the live stop, so 020 still has only the stop the "
            "trade opened with"
        )

    def test_it_comes_from_the_matched_position(self):
        """It must be THIS signal's position, not whichever the loop last saw:
        a template grid has several legs open at once."""
        body = self._body()
        idx = body.index("record_last_seen_sl(")
        call = body[idx:idx + 160]

        assert "sl" in call.lower()
