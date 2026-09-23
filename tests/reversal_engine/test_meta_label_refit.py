"""The meta-labeller gets trained, once a day, from the signals already closed.

Until 2026-09-22 nothing in the app ever called `MetaLabeller.fit`, so its
status read "never fitted" forever -- the AI tuner switched its gate off on
2026-09-15 for exactly that reason. These pin the two new pieces: the rows it
is trained on (`meta_label.rows_from_signals`) and the daily refit
(`meta_label_schedule.meta_label_refit_sweep`).

Nothing here places, closes or modifies a trade. Whether the live path
CONSULTS the model is still `meta_label_gate_enabled`, which this does not
touch.
"""
import asyncio
import json
from datetime import datetime
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.reversal_engine import meta_label as ml
from backend.src.services.reversal_engine import meta_label_schedule as sched
from backend.src.services.reversal_engine.ml_engine import FEATURE_NAMES


def _closed(**over):
    row = {
        "id": 1, "created_at": 1000.0, "trigger_time": 1060.0,
        "close_time": 1600.0, "outcome": "win", "sl_dist": 5.0,
        "net_pnl_dollars": 25.0,
        "ml_features_json": json.dumps([0.1] * len(FEATURE_NAMES)),
    }
    row.update(over)
    return row


class TestTheTrainingRows:
    def test_a_closed_signal_becomes_one_row_labelled_in_realised_r(self):
        rows = ml.rows_from_signals([_closed()])
        assert len(rows) == 1
        # $25 net on a 5-point stop at the virtual 0.1 lot = 25 / 50 = 0.5R
        assert rows[0]["realised_r"] == pytest.approx(0.5)

    def test_cost_is_zero_because_net_pnl_already_charges_it(self):
        """`net_pnl_dollars` is after spread, commission and slippage
        (`reversal_engine_manage._net_pnl`, and the broker's own figure on a
        reconciled live trade). Charging a TCA cost on top would count the
        cost twice. So a trade that netted a cent is a one, and one that
        netted minus a cent is a zero."""
        up, down = ml.rows_from_signals([
            _closed(net_pnl_dollars=0.01), _closed(net_pnl_dollars=-0.01)])
        assert up["cost_r"] == 0.0
        assert ml.label_for(up["realised_r"], up["cost_r"]) == 1
        assert ml.label_for(down["realised_r"], down["cost_r"]) == 0

    def test_the_span_runs_from_the_fill_not_the_creation(self):
        """The purge needs when the trade was exposed to the market. A zone
        signal can wait hours before it fills."""
        row = ml.rows_from_signals([_closed()])[0]
        assert (row["open_time"], row["close_time"]) == (1060.0, 1600.0)

    def test_an_unfilled_signal_spans_from_its_creation(self):
        row = ml.rows_from_signals([_closed(trigger_time=None)])[0]
        assert row["open_time"] == 1000.0

    def test_an_older_shorter_vector_is_padded_to_the_current_width(self):
        row = ml.rows_from_signals(
            [_closed(ml_features_json=json.dumps([0.1] * 10))])[0]
        assert len(row["features"]) == len(FEATURE_NAMES)

    @pytest.mark.parametrize("bad", [
        {"outcome": "open"},
        {"close_time": None},
        {"ml_features_json": None},
        {"ml_features_json": "not json"},
        {"sl_dist": 0.0},
        {"net_pnl_dollars": None},
    ])
    def test_a_row_that_cannot_be_labelled_is_left_out(self, bad):
        assert ml.rows_from_signals([_closed(**bad)]) == []


def _learnable_signals(n=400):
    """Feature 0 decides the outcome, so a fit on these arms."""
    out = []
    for i in range(n):
        good = i % 2 == 0
        feats = [0.0] * len(FEATURE_NAMES)
        feats[0] = 1.0 + (i % 7) * 0.01 if good else -1.0 - (i % 5) * 0.01
        out.append(_closed(id=i, created_at=i * 10_000.0,
                           trigger_time=i * 10_000.0,
                           close_time=i * 10_000.0 + 600.0,
                           outcome="win" if good else "loss",
                           net_pnl_dollars=40.0 if good else -50.0,
                           ml_features_json=json.dumps(feats)))
    return out


def _run(now, *, loader):
    asyncio.run(sched.meta_label_refit_sweep(None, now=now, loader=loader))


@pytest.fixture(autouse=True)
def _fresh_instance():
    """The model and the last-fit date are process state. Every test starts
    from a never-fitted model and no fit today."""
    ml._instance = None
    sched._last_fit_date = None
    sched._last_toggle = None
    yield
    ml._instance = None
    sched._last_fit_date = None
    sched._last_toggle = None


_DAY1 = datetime(2026, 9, 22, 22, 30)
_DAY1_LATER = datetime(2026, 9, 22, 23, 50)
_DAY2 = datetime(2026, 9, 23, 0, 5)


class TestTheDailyRefit:
    def test_before_anything_runs_the_model_has_never_been_fitted(self):
        """Negative control for every test below."""
        assert ml.get_instance().status.refusal == "never fitted"

    def test_it_fits_the_live_instance_from_history(self):
        _run(_DAY1, loader=_learnable_signals)
        st = ml.get_instance().status
        assert st.ready is True
        assert st.n_samples == 400
        assert st.auc_oos is not None and st.auc_oos > 0.9

    def test_it_fits_at_any_hour_so_a_restart_is_not_left_unfitted(self):
        _run(datetime(2026, 9, 22, 9, 0), loader=_learnable_signals)
        assert ml.get_instance().status.n_samples == 400

    def test_it_fits_once_per_london_day(self):
        calls = []

        def _loader():
            calls.append(1)
            return _learnable_signals()

        _run(_DAY1, loader=_loader)
        _run(_DAY1_LATER, loader=_loader)
        assert len(calls) == 1
        _run(_DAY2, loader=_loader)
        assert len(calls) == 2

    def test_a_refusal_is_still_a_fit_and_is_recorded_as_one(self):
        """A model that cannot beat a coin is an answer, not a failure. The
        day is done and the status says why it did not arm."""
        _run(_DAY1, loader=lambda: _learnable_signals()[:50])
        st = ml.get_instance().status
        assert st.ready is False
        assert "below the" in st.refusal
        assert sched._last_fit_date == "2026-09-22"

    def test_it_asks_nothing_about_the_node_role(self):
        """A remote node's engine returns before generating anything, so
        nothing there consults the model, and the timer's one node-role
        check belongs to the research sweep (see
        tests/core/test_reversal_research_characterization.py)."""
        calls = []
        with mock.patch.object(db, "is_remote_node",
                               side_effect=lambda: calls.append(1) or True):
            asyncio.run(sched.meta_label_refit_sweep(
                None, now=_DAY1, loader=_learnable_signals))
        assert calls == []
        assert ml.get_instance().status.n_samples == 400

    def test_a_failed_read_leaves_the_model_alone_and_does_not_claim_the_day(self):
        _run(_DAY1, loader=_learnable_signals)
        before = ml.get_instance()

        def _broken():
            raise RuntimeError("database is locked")

        _run(_DAY2, loader=_broken)
        assert ml.get_instance() is before
        assert sched._last_fit_date == "2026-09-22"

    def test_the_new_model_replaces_the_old_one_whole(self):
        """Fitted on a fresh object and swapped in, so the live path never
        reads a model whose status and weights come from different fits."""
        _run(_DAY1, loader=_learnable_signals)
        first = ml.get_instance()
        _run(_DAY2, loader=_learnable_signals)
        assert ml.get_instance() is not first
        assert ml.get_instance().status.ready is True


class TestItIsOnTheTimer:
    def test_the_research_loop_runs_the_refit(self):
        called = []

        async def _noop(engine):
            return None

        async def _refit(engine):
            called.append(engine)

        engine = object()
        running = {"n": 0}

        def _is_running():
            running["n"] += 1
            return running["n"] <= 1

        loop = "backend.src.services.reversal_engine.research_loop"

        async def _go():
            with mock.patch("asyncio.sleep", new=mock.AsyncMock()), \
                 mock.patch(f"{loop}._reversal_engine_research_sweep_impl",
                            side_effect=_noop), \
                 mock.patch(f"{loop}._reversal_engine_study_sweep_impl",
                            side_effect=_noop), \
                 mock.patch(f"{loop}._breakout_excursion_sweep_impl",
                            side_effect=_noop), \
                 mock.patch(f"{loop}._meta_label_refit_sweep_impl",
                            side_effect=_refit):
                from backend.src.services.reversal_engine import research_loop
                await research_loop.reversal_engine_research_loop(engine, _is_running)

        asyncio.run(_go())
        assert called == [engine]


# ── Cross-asset features (docs/todo/reversal-engine/230) ─────────────────────

from backend.src.services.reversal_engine import cross_asset as xa  # noqa: E402


def _xasset(good):
    feats = {n: 0.0 for n in xa.FEATURE_NAMES}
    feats["XAGUSD_z60"] = 2.0 if good else -2.0
    return json.dumps({"v": 1, "features": feats})


def _signals_with_xasset(n=400, informative_base=False):
    """Outcome decided by silver's move; the base features carry nothing
    unless `informative_base`."""
    out = []
    for i in range(n):
        good = i % 2 == 0
        feats = [0.0] * len(FEATURE_NAMES)
        feats[1] = (i % 11) * 0.1                       # noise
        if informative_base:
            feats[0] = 1.0 if good else -1.0
        out.append(_closed(id=i, created_at=i * 10_000.0, trigger_time=i * 10_000.0,
                           close_time=i * 10_000.0 + 600.0,
                           outcome="win" if good else "loss",
                           net_pnl_dollars=40.0 if good else -50.0,
                           ml_features_json=json.dumps(feats),
                           xasset_json=_xasset(good)))
    return out


class TestCrossAssetRows:
    def test_the_vector_is_the_base_features_then_the_peers(self):
        row = ml.rows_from_signals([_closed(xasset_json=_xasset(True))], xasset=True)[0]
        assert len(row["features"]) == len(FEATURE_NAMES) + len(xa.FEATURE_NAMES)
        assert row["features"][len(FEATURE_NAMES) + xa.FEATURE_NAMES.index("XAGUSD_z60")] == 2.0

    def test_a_row_never_measured_is_left_out_of_the_cross_asset_set(self):
        assert ml.rows_from_signals([_closed()], xasset=True) == []

    def test_the_base_set_ignores_the_cross_asset_column(self):
        row = ml.rows_from_signals([_closed(xasset_json=_xasset(True))])[0]
        assert len(row["features"]) == len(FEATURE_NAMES)


def _run_x(now, *, loader, toggle, recorded=None):
    async def _settings():
        return {"re_xasset_features_enabled": 1 if toggle else 0}

    def _record(**kw):
        if recorded is not None:
            recorded.append(kw)

    asyncio.run(sched.meta_label_refit_sweep(
        None, now=now, loader=loader, settings_reader=_settings, recorder=_record))


class TestBothVariantsAreMeasured:
    def test_every_fit_records_both_aucs_on_the_same_rows(self):
        rec = []
        _run_x(_DAY1, loader=_signals_with_xasset, toggle=False, recorded=rec)
        assert len(rec) == 1
        r = rec[0]
        assert r["n"] == 400
        assert r["auc_xasset"] > 0.9          # silver decides the outcome
        assert r["auc_base"] < 0.7            # the base features carry nothing
        assert r["installed"] == "base"

    def test_each_peer_gets_its_own_score(self):
        rec = []
        _run_x(_DAY1, loader=_signals_with_xasset, toggle=False, recorded=rec)
        peers = rec[0]["per_peer"]
        assert set(peers) == set(xa.PEERS)
        assert peers["XAGUSD"]["auc_z60"] > 0.9
        assert peers["XAGUSD"]["n"] == 400

    def test_no_measured_rows_records_nothing(self):
        rec = []
        _run_x(_DAY1, loader=_learnable_signals, toggle=True, recorded=rec)
        assert rec == []


class TestTheToggleChoosesTheModel:
    def test_off_installs_the_base_model(self):
        _run_x(_DAY1, loader=_signals_with_xasset, toggle=False)
        assert ml.get_instance().uses_xasset is False

    def test_on_installs_the_cross_asset_model(self):
        _run_x(_DAY1, loader=_signals_with_xasset, toggle=True)
        inst = ml.get_instance()
        assert inst.uses_xasset is True
        assert inst.status.ready is True

    def test_flipping_the_toggle_refits_without_waiting_a_day(self):
        calls = []

        def _loader():
            calls.append(1)
            return _signals_with_xasset()

        _run_x(_DAY1, loader=_loader, toggle=False)
        _run_x(_DAY1_LATER, loader=_loader, toggle=False)
        assert len(calls) == 1
        _run_x(_DAY1_LATER, loader=_loader, toggle=True)
        assert len(calls) == 2
        assert ml.get_instance().uses_xasset is True

    def test_an_unreadable_setting_reads_as_off(self):
        async def _broken():
            raise RuntimeError("no settings table")

        asyncio.run(sched.meta_label_refit_sweep(
            None, now=_DAY1, loader=_signals_with_xasset, settings_reader=_broken,
            recorder=lambda **kw: None))
        assert ml.get_instance().uses_xasset is False


class TestScoringWithTheCrossAssetModel:
    def test_a_signal_with_its_features_is_scored(self, monkeypatch):
        _run_x(_DAY1, loader=_signals_with_xasset, toggle=True)
        good = _signals_with_xasset(2)[0]
        monkeypatch.setattr("backend.src.services.reversal_engine.ml_engine.extract_features",
                            lambda sig, wr: json.loads(sig["ml_features_json"]))
        monkeypatch.setattr("backend.src.services.reversal_engine.reversal_engine_repo"
                            ".get_recent_win_rate", lambda n: 0.5)
        p = ml.score_signal(good)
        assert p is not None and p > 0.5

    def test_a_signal_not_yet_measured_gets_no_opinion(self, monkeypatch):
        _run_x(_DAY1, loader=_signals_with_xasset, toggle=True)
        sig = _signals_with_xasset(2)[0]
        sig["xasset_json"] = None
        monkeypatch.setattr("backend.src.services.reversal_engine.ml_engine.extract_features",
                            lambda s, wr: json.loads(s["ml_features_json"]))
        monkeypatch.setattr("backend.src.services.reversal_engine.reversal_engine_repo"
                            ".get_recent_win_rate", lambda n: 0.5)
        assert ml.score_signal(sig) is None
