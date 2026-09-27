"""The Breakout engine's tuning experiment ledger (docs/todo/007).

Every AI adjustment from the batch review becomes a recorded experiment.

Record mode (the default) must behave exactly as before: every adjustment is
applied as it arrives. The ledger only writes down what happened and, once
enough signals have closed, whether it looks better or worse. It never rolls
anything back in this mode.

Approve mode turns adjustments into proposals that change nothing until the
owner approves one. Only one runs at a time. After `MIN_SAMPLE` closed signals
it is judged against the baseline captured when it started, and a worse result
is rolled back. The failure line rolls back early. A param a human has since
changed is abandoned rather than rolled back over their edit.

Nothing here reaches a broker: the engine's own virtual signals are seeded
straight into a private breakout database.
"""
import os
import tempfile
import time

import pytest

from backend.src.services.breakout_signal import adaptive_params as ap
from backend.src.services.breakout_signal import breakout_signal_repo as repo
from backend.src.services.breakout_signal import tuning_ledger as ledger
from tests.conftest import remove_db_file

_PARAM = "min_adx_go"          # default 28.0, range 20-45
_OTHER = "min_adx_retest"      # default 24.0, range 18-40
_LOCKED = "daily_loss_stop_usd"


@pytest.fixture
def bo_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    repo.close_db()
    remove_db_file(path)


def _close(n, net, at=None):
    """Seed `n` closed virtual signals, each netting `net` dollars."""
    at = at if at is not None else time.time()
    for i in range(n):
        sid = repo.create_signal({
            "direction": "BUY", "breakout_type": "go", "entry_mid": 2400.0,
            "stop_loss": 2390.0, "signal_ref": f"BO-{time.time_ns()}-{i}",
        })
        repo.get_db().run(
            "UPDATE bo_signals SET status='closed', outcome=?, close_time=?, "
            "net_pnl_dollars=? WHERE id=?",
            "win" if net > 0 else "loss", at + i * 0.001, net, sid,
        )


def _adj(param, value, reason="because"):
    return {"param": param, "new_value": value, "reason": reason}


def _by_status(status):
    return [e for e in ledger.state()["history"] + ledger.state()["proposals"]
            + ([ledger.state()["running"]] if ledger.state()["running"] else [])
            if e["status"] == status]


# ── Record mode: exactly today's behaviour, now written down ────────────────

def test_record_mode_is_the_default(bo_db):
    assert ledger.approval_required() is False


def test_record_mode_applies_every_adjustment_as_before(bo_db):
    applied = ledger.handle_batch([_adj(_PARAM, 30.0), _adj(_OTHER, 26.0)], "summary")
    assert ap.get(_PARAM) == 30.0
    assert ap.get(_OTHER) == 26.0
    assert applied == [f"{_PARAM}→30", f"{_OTHER}→26"]


def test_record_mode_flags_a_batch_that_changed_several_things(bo_db):
    ledger.handle_batch([_adj(_PARAM, 30.0), _adj(_OTHER, 26.0)], "s")
    rows = _by_status("applied_auto")
    assert len(rows) == 2
    assert all(r["concurrent"] == 2 for r in rows)


def test_record_mode_single_change_is_not_flagged(bo_db):
    ledger.handle_batch([_adj(_PARAM, 30.0)], "s")
    assert _by_status("applied_auto")[0]["concurrent"] == 1


def test_record_mode_records_nothing_for_a_refused_or_no_op_change(bo_db):
    ledger.handle_batch([_adj(_LOCKED, 300.0), _adj(_PARAM, 28.0)], "s")
    assert ledger.state()["history"] == []


def test_record_mode_judges_but_never_rolls_back(bo_db):
    _close(ledger.MIN_SAMPLE, 10.0, at=time.time() - 10_000)
    ledger.handle_batch([_adj(_PARAM, 30.0)], "s")
    _close(ledger.MIN_SAMPLE, -5.0, at=time.time() + 1)
    ledger.evaluate()
    row = _by_status("judged")[0]
    assert row["verdict"] == "worse"
    assert ap.get(_PARAM) == 30.0


# ── Approve mode ────────────────────────────────────────────────────────────

@pytest.fixture
def approve(bo_db):
    ledger.set_approval_required(True)
    return bo_db


def test_a_proposal_changes_nothing(approve):
    assert ledger.handle_batch([_adj(_PARAM, 35.0)], "s") == []
    assert ap.get(_PARAM) == 28.0
    assert [p["param"] for p in ledger.state()["proposals"]] == [_PARAM]


def test_a_proposal_is_stored_at_the_value_the_clamp_would_apply(approve):
    ledger.handle_batch([_adj(_PARAM, 99.0)], "s")
    assert ledger.state()["proposals"][0]["new_value"] == 45.0


def test_a_locked_param_is_never_proposed(approve):
    ledger.handle_batch([_adj(_LOCKED, 300.0), _adj("not_a_param", 1.0)], "s")
    assert ledger.state()["proposals"] == []


def test_a_new_batch_supersedes_the_old_proposals(approve):
    ledger.handle_batch([_adj(_PARAM, 35.0)], "first")
    ledger.handle_batch([_adj(_OTHER, 30.0)], "second")
    assert [p["param"] for p in ledger.state()["proposals"]] == [_OTHER]
    assert [e["param"] for e in _by_status("superseded")] == [_PARAM]


def test_approving_applies_it_and_captures_the_baseline(approve):
    _close(5, 12.0, at=time.time() - 10_000)
    ledger.handle_batch([_adj(_PARAM, 35.0)], "s")
    pid = ledger.state()["proposals"][0]["id"]

    ledger.approve(pid)

    assert ap.get(_PARAM) == 35.0
    running = ledger.state()["running"]
    assert running["id"] == pid
    assert running["old_value"] == 28.0
    assert running["baseline_n"] == 5
    assert running["baseline_mean"] == 12.0


def test_only_one_experiment_runs_at_a_time(approve):
    ledger.handle_batch([_adj(_PARAM, 35.0), _adj(_OTHER, 30.0)], "s")
    first, second = [p["id"] for p in ledger.state()["proposals"]]
    ledger.approve(first)
    with pytest.raises(ValueError, match="already running"):
        ledger.approve(second)
    assert ap.get(_OTHER) == 24.0


def test_approving_something_not_proposed_is_refused(approve):
    with pytest.raises(ValueError):
        ledger.approve(12345)


def test_rejecting_a_proposal_changes_nothing(approve):
    ledger.handle_batch([_adj(_PARAM, 35.0)], "s")
    pid = ledger.state()["proposals"][0]["id"]
    ledger.reject(pid)
    assert ap.get(_PARAM) == 28.0
    assert _by_status("rejected")[0]["id"] == pid


def _run_one(before_net, after_net, after_n=None):
    _close(ledger.MIN_SAMPLE, before_net, at=time.time() - 10_000)
    ledger.handle_batch([_adj(_PARAM, 35.0)], "s")
    ledger.approve(ledger.state()["proposals"][0]["id"])
    _close(after_n if after_n is not None else ledger.MIN_SAMPLE, after_net,
           at=time.time() + 1)
    ledger.evaluate()


def test_worse_after_the_sample_is_rolled_back(approve):
    _run_one(before_net=5.0, after_net=1.0)
    assert ap.get(_PARAM) == 28.0
    assert _by_status("rolled_back")[0]["verdict"] == "worse"


def test_not_worse_after_the_sample_is_kept(approve):
    _run_one(before_net=1.0, after_net=1.0)
    assert ap.get(_PARAM) == 35.0
    assert _by_status("kept")[0]["param"] == _PARAM
    assert ledger.state()["running"] is None


def test_short_of_the_sample_it_keeps_running(approve):
    _run_one(before_net=5.0, after_net=1.0, after_n=ledger.MIN_SAMPLE - 1)
    assert ledger.state()["running"] is not None
    assert ap.get(_PARAM) == 35.0


def test_the_failure_line_rolls_back_early(approve):
    per_trade = -(ledger.FAILURE_USD / 4) - 1
    _run_one(before_net=5.0, after_net=per_trade, after_n=4)
    assert ap.get(_PARAM) == 28.0
    assert _by_status("rolled_back")[0]["verdict"] == "failure_line"


def test_a_small_loss_short_of_the_line_does_not(approve):
    _run_one(before_net=5.0, after_net=-1.0, after_n=4)
    assert ledger.state()["running"] is not None


def test_a_human_edit_abandons_the_experiment_and_is_left_alone(approve):
    _close(ledger.MIN_SAMPLE, 5.0, at=time.time() - 10_000)
    ledger.handle_batch([_adj(_PARAM, 35.0)], "s")
    ledger.approve(ledger.state()["proposals"][0]["id"])
    ap.apply_adjustment(_PARAM, 40.0, "owner edit")
    _close(ledger.MIN_SAMPLE, -5.0, at=time.time() + 1)

    ledger.evaluate()

    assert ap.get(_PARAM) == 40.0
    assert _by_status("abandoned")[0]["param"] == _PARAM


def test_turning_approval_off_leaves_a_running_experiment_to_finish(approve):
    _close(ledger.MIN_SAMPLE, 5.0, at=time.time() - 10_000)
    ledger.handle_batch([_adj(_PARAM, 35.0)], "s")
    ledger.approve(ledger.state()["proposals"][0]["id"])
    ledger.set_approval_required(False)
    _close(ledger.MIN_SAMPLE, 1.0, at=time.time() + 1)
    ledger.evaluate()
    assert ap.get(_PARAM) == 28.0   # its rollback contract still holds


def test_state_reports_the_running_experiments_progress(approve):
    _run_one(before_net=5.0, after_net=2.0, after_n=3)
    run = ledger.state()["running"]
    assert run["after_n"] == 3
    assert run["after_mean"] == 2.0
    assert run["after_sum"] == 6.0
