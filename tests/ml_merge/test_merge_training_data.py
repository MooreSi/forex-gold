"""Merging labelled training rows between the Mac and the VPS (docs/todo/026).

The owner switches which node trades, so each node's history covers only the
periods it generated. A merge copies the other node's labelled rows into a
separate table, `ml_imported_rows`, and the training readers include them
only when a benefit check on the most recent rows says they do not make the
model worse.

What these pin:
  * a switch that is off (the default) leaves every training set exactly as
    it was, even with imported rows sitting in the table;
  * imports are idempotent and never accept rows that came from this node;
  * the same market event recorded by both nodes is counted once;
  * executed Reversal rows are not exported (their label needs this node's
    core trade);
  * the live tables (`re_signals`, `bo_signals`, `pro_snapshots`) are never
    written;
  * the benefit check refuses a merge that makes the gate worse.

Temp databases. Nothing here reaches a broker or the sync link.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.breakout_signal import breakout_signal_repo as bdb
from backend.src.services.breakout_signal import ml_engine as bo_ml
from backend.src.services.ml_merge import benefit, service, transfer, union
from backend.src.services.ml_merge import repo as mrepo
from backend.src.services.reversal_engine import pro_corpus_repo as pro_corpus
from backend.src.services.reversal_engine import pro_model
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from backend.src.services.reversal_engine.ml_engine import _training_data as td
from tests.ml_merge.conftest import (OWN, PEER, add_bo, add_re, bo_row,
                                     pro_row, re_row)


def _peer_re_rows(n: int, start: float = 1_000_000.0, **kw) -> list[dict]:
    """Rows as the peer exports them: its own ids, full columns. Well after
    any own row in these tests, so none is taken for the same event."""
    return [{**re_row(f"RE-P{i}", start + i * 1000, **kw), "id": i + 1}
            for i in range(n)]


# ── The default changes nothing ──────────────────────────────────────────────

class TestASwitchThatIsOffChangesNothing:

    def test_reversal_training_set_is_identical_with_rows_imported_but_unused(self, dbs):
        for i in range(20):
            add_re(re_row(f"RE-{i}", 1_000.0 + i * 1000, win=i % 2 == 0))
        before = td._get_training_set()

        service.import_rows("re", PEER, _peer_re_rows(20), own_node=OWN)

        assert td._get_training_set() == before

    def test_the_negative_control_turning_it_on_adds_the_rows(self, dbs):
        for i in range(20):
            add_re(re_row(f"RE-{i}", 1_000.0 + i * 1000))
        service.import_rows("re", PEER, _peer_re_rows(20), own_node=OWN)

        mrepo.set_decision("re", True, {})

        assert len(td._get_training_set()[0]) == 40

    def test_breakout_rows_are_identical_while_off(self, dbs):
        for i in range(5):
            add_bo(bo_row(f"BO-{i}", 1_000.0 + i * 1000))
        before = bo_ml._training_rows()
        peer = [{**bo_row(f"BO-P{i}", 90_000.0 + i * 1000), "id": i + 1} for i in range(5)]

        service.import_rows("bo", PEER, peer, own_node=OWN)

        assert bo_ml._training_rows() == before
        mrepo.set_decision("bo", True, {})
        assert len(bo_ml._training_rows()) == 10

    def test_pro_corpus_is_identical_while_off(self, dbs):
        pro_corpus.insert(pro_row("m-1", "complete", 1_000.0))
        pro_corpus.insert(pro_row("bg-1-BUY", "background", 2_000.0, rsi=70))
        before = pro_model._dataset()
        peer = [{**pro_row("m-2", "complete", 50_000.0), "id": 1},
                {**pro_row("bg-9-SELL", "background", 60_000.0, direction="SELL"), "id": 2}]

        service.import_rows("pro", PEER, peer, own_node=OWN)

        assert pro_model._dataset() == before
        mrepo.set_decision("pro", True, {})
        assert len(pro_model._dataset()[1]) == 4


# ── Importing ────────────────────────────────────────────────────────────────

class TestImporting:

    def test_a_second_import_of_the_same_rows_adds_nothing(self, dbs):
        rows = _peer_re_rows(3)
        assert service.import_rows("re", PEER, rows, own_node=OWN) == 3
        assert service.import_rows("re", PEER, rows, own_node=OWN) == 0
        assert mrepo.count("re") == 3

    def test_rows_that_came_from_this_node_are_refused(self, dbs):
        with pytest.raises(ValueError):
            service.import_rows("re", OWN, _peer_re_rows(1), own_node=OWN)
        assert mrepo.count("re") == 0

    def test_an_unknown_kind_is_refused(self, dbs):
        with pytest.raises(ValueError):
            service.import_rows("re_signals; DROP", PEER, _peer_re_rows(1), own_node=OWN)

    def test_a_row_without_an_integer_id_is_skipped(self, dbs):
        rows = [{**re_row("RE-X", 1.0), "id": "7"}, {**re_row("RE-Y", 2.0), "id": 8}]
        assert service.import_rows("re", PEER, rows, own_node=OWN) == 1

    def test_the_live_tables_are_never_written(self, dbs):
        add_re(re_row("RE-1", 1.0))
        add_bo(bo_row("BO-1", 1.0))
        pro_corpus.insert(pro_row("m-1", "complete", 1.0))

        service.import_rows("re", PEER, _peer_re_rows(4), own_node=OWN)
        service.import_rows("bo", PEER, [{**bo_row("BO-P", 9.0), "id": 1}], own_node=OWN)
        service.import_rows("pro", PEER, [{**pro_row("m-9", "complete", 9.0), "id": 1}],
                            own_node=OWN)
        for kind in ("re", "bo", "pro"):
            mrepo.set_decision(kind, True, {})

        assert re_repo.get_db().get("SELECT COUNT(*) FROM re_signals")[0] == 1
        assert bdb.get_db().get("SELECT COUNT(*) FROM bo_signals")[0] == 1
        assert re_repo.get_db().get("SELECT COUNT(*) FROM pro_snapshots")[0] == 1


# ── Exporting ────────────────────────────────────────────────────────────────

class TestExporting:

    def test_executed_and_open_reversal_rows_are_not_exported(self, dbs):
        add_re(re_row("RE-V", 1.0))
        add_re(re_row("RE-E", 2.0, executed=True))
        add_re(re_row("RE-O", 3.0, status="pending"))

        page = service.export_page("re", after=0, limit=10)

        assert [r["signal_ref"] for r in page["rows"]] == ["RE-V"]
        assert page["done"] is True

    def test_pages_follow_the_id_cursor(self, dbs):
        for i in range(5):
            add_re(re_row(f"RE-{i}", float(i)))

        first = service.export_page("re", after=0, limit=2)
        second = service.export_page("re", after=first["next_after"], limit=10)

        assert [r["signal_ref"] for r in first["rows"]] == ["RE-0", "RE-1"]
        assert first["done"] is False
        assert [r["signal_ref"] for r in second["rows"]] == ["RE-2", "RE-3", "RE-4"]

    def test_imported_rows_are_never_exported_again(self, dbs):
        service.import_rows("re", PEER, _peer_re_rows(3), own_node=OWN)
        assert service.export_page("re", after=0, limit=10)["rows"] == []


# ── The same event on both nodes ─────────────────────────────────────────────

class TestTheSameEventCountsOnce:

    def _union_refs(self, own, peer):
        for r in own:
            add_re(r)
        service.import_rows("re", PEER, peer, own_node=OWN)
        mrepo.set_decision("re", True, {})
        rows = sorted(re_repo.get_ml_training_data(), key=lambda r: r["id"])
        return [r["signal_ref"] for r in union.with_imported("re", rows)]

    def test_a_reversal_signal_both_nodes_recorded_is_kept_once(self, dbs):
        refs = self._union_refs([re_row("RE-OWN", 1_000.0, level=4100.0)],
                                [{**re_row("RE-PEER", 1_120.0, level=4100.5), "id": 1}])
        assert refs == ["RE-OWN"]

    def test_301_seconds_apart_is_a_different_signal(self, dbs):
        refs = self._union_refs([re_row("RE-OWN", 1_000.0)],
                                [{**re_row("RE-PEER", 1_301.0), "id": 1}])
        assert refs == ["RE-OWN", "RE-PEER"]

    def test_the_other_direction_is_a_different_signal(self, dbs):
        refs = self._union_refs([re_row("RE-OWN", 1_000.0)],
                                [{**re_row("RE-PEER", 1_000.0, direction="SELL"), "id": 1}])
        assert sorted(refs) == ["RE-OWN", "RE-PEER"]

    def test_a_pro_post_both_nodes_captured_is_kept_once(self, dbs):
        pro_corpus.insert(pro_row("m-1", "complete", 1_000.0))
        pro_corpus.insert(pro_row("bg-1000-BUY", "background", 1_000.0))
        service.import_rows("pro", PEER, [
            {**pro_row("m-1", "complete", 1_005.0), "id": 1},
            {**pro_row("bg-1100-BUY", "background", 1_100.0), "id": 2},
            {**pro_row("bg-1100-SELL", "background", 1_100.0, direction="SELL"), "id": 3},
        ], own_node=OWN)
        mrepo.set_decision("pro", True, {})

        _X, y, _w, _ = pro_model._dataset()

        assert sum(y) == 1                  # one positive: m-1 once
        assert len(y) - sum(y) == 2         # own BUY bg + the peer's SELL bg


# ── Is it worth it? ──────────────────────────────────────────────────────────

def _timeline(dbs, peer_agrees: bool) -> None:
    """The peer's 400 rows are older than this node's 150. Own rows follow
    x > 0.5 => win. The peer's follow the same rule, or the opposite one: a
    market that has since changed. The newest 20% of the union (110 rows) is
    all own rows, so the check asks "does the old history help on today's"."""
    peer = []
    for i in range(400):
        x = (i * 53 % 100) / 100.0
        win = (x > 0.5) if peer_agrees else (x <= 0.5)
        peer.append({**re_row(f"RE-P{i}", i * 1000.0, x=x, win=win), "id": i + 1})
    for i in range(150):
        x = (i * 37 % 100) / 100.0
        add_re(re_row(f"RE-{i}", 1_000_000.0 + i * 1000.0, x=x, win=x > 0.5))
    service.import_rows("re", PEER, peer, own_node=OWN)


class TestTheBenefitCheck:

    def test_rows_that_contradict_the_recent_ones_are_kept_out(self, dbs):
        _timeline(dbs, peer_agrees=False)

        report = benefit.evaluate("re")

        assert report["use"] is False
        assert report["b"] < report["a"]
        assert mrepo.use_imported("re") is False
        assert len(td._get_training_set()[0]) == 150

    def test_rows_that_agree_are_taken_in(self, dbs):
        _timeline(dbs, peer_agrees=True)

        report = benefit.evaluate("re")

        assert report["use"] is True
        assert mrepo.use_imported("re") is True
        assert len(td._get_training_set()[0]) == 550

    def test_with_nothing_imported_the_switch_stays_off(self, dbs):
        for i in range(60):
            add_re(re_row(f"RE-{i}", i * 1000.0, x=i / 60, win=i % 2 == 0))
        report = benefit.evaluate("re")
        assert report["use"] is False
        assert "nothing" in report["reason"]

    def test_the_decision_rule(self):
        assert benefit.decide(a=0.10, b=0.10, neutral=0.0) is True
        assert benefit.decide(a=0.10, b=0.09, neutral=0.0) is False
        # No own model to compare with: the merged one must beat doing nothing.
        assert benefit.decide(a=None, b=0.01, neutral=0.0) is True
        assert benefit.decide(a=None, b=-0.01, neutral=0.0) is False
        assert benefit.decide(a=0.6, b=None, neutral=0.5) is False

    def test_too_little_data_to_measure_keeps_breakout_and_pro_off(self, dbs):
        add_bo(bo_row("BO-1", 1.0))
        service.import_rows("bo", PEER, [{**bo_row("BO-P", 9_999.0), "id": 1}], own_node=OWN)
        pro_corpus.insert(pro_row("m-1", "complete", 1.0))
        service.import_rows("pro", PEER, [{**pro_row("m-2", "complete", 9.0), "id": 1}],
                            own_node=OWN)

        for kind in ("bo", "pro"):
            report = benefit.evaluate(kind)
            assert report["use"] is False, kind
            assert mrepo.use_imported(kind) is False


# ── The wire ─────────────────────────────────────────────────────────────────

class TestChunking:

    def test_every_chunk_fits_and_nothing_is_lost(self):
        rows = [{"id": i, "blob": "x" * 900} for i in range(1000)]

        chunks = transfer.chunk_rows(rows, max_bytes=64 * 1024)

        assert all(len(json.dumps(c)) <= 64 * 1024 for c in chunks)
        assert [r["id"] for c in chunks for r in c] == list(range(1000))
        assert len(chunks) > 1

    def test_a_row_bigger_than_the_limit_travels_alone(self):
        rows = [{"id": 1, "blob": "x" * 2000}, {"id": 2}]
        chunks = transfer.chunk_rows(rows, max_bytes=1000)
        assert [[r["id"] for r in c] for c in chunks] == [[1], [2]]
