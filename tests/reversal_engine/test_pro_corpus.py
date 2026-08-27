"""Storage contract for the reference-channel learning corpus.

Expected behaviour, from the feature spec rather than from the code:

  * The corpus lives in the Reversal Engine's shared database, so it does not
    split when the app switches between the demo and live account.
  * One row per (message, stage). The capture poller runs every 5s and can
    see the same signal twice; the second write must be a no-op, not a
    duplicate training row.
  * Only positives with complete stated levels are offered to the outcome
    resolver -- background samples have no levels to judge, and a signal
    captured seconds ago has no forward candles yet.
  * Once resolved, a row leaves the pending queue.
"""
import os
import tempfile
import time

import pytest

from backend.src.services.reversal_engine import pro_corpus_repo as pro_corpus
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo


@pytest.fixture
def corpus():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    pro_corpus.create_schema()
    yield pro_corpus
    os.remove(path)


def snapshot(msg_id="msg-1", stage="complete", **over):
    row = {
        "tg_message_id": msg_id, "stage": stage,
        "group_name": "Gold Diggers VIP", "direction": "BUY",
        "signal_ts": time.time() - 3600, "captured_at": time.time() - 3595,
        "capture_lag_s": 5.0,
        "entry_low": 4100.0, "entry_high": 4102.0,
        "stop_loss": 4090.0, "tp1": 4120.0,
        "bid": 4101.0, "ask": 4101.3, "spread_points": 30.0, "price": 4101.15,
        "dist_to_entry_mid": 0.15, "price_inside_zone": 1,
        "session": "london", "regime_score": 1.0,
        "indicators_json": '{"M15": {"rsi14": 61.2, "adx14": 28.4, "atr14": 6.1}}',
        "fvg_json": '{"fvg_confluence": 1.0, "fvg_dist_norm": 0.4, '
                    '"fvg_fresh": 1.0, "fvg_size_norm": 0.8}',
        "raw_text": "BUY GOLD 4100-4102 SL 4090 TP 4120",
    }
    row.update(over)
    return row


def test_a_captured_signal_is_stored_and_counted_as_a_positive(corpus):
    corpus.insert(snapshot())

    assert corpus.counts()["pos"] == 1
    assert corpus.counts()["neg"] == 0


def test_a_background_sample_is_counted_as_a_negative(corpus):
    corpus.insert(snapshot(msg_id="bg-1", stage="background",
                           entry_low=None, entry_high=None,
                           stop_loss=None, tp1=None))

    assert corpus.counts()["neg"] == 1
    assert corpus.counts()["pos"] == 0


def test_the_same_message_and_stage_cannot_be_stored_twice(corpus):
    corpus.insert(snapshot())

    written_again = corpus.insert(snapshot())

    assert written_again is False
    assert corpus.counts()["pos"] == 1


def test_the_two_stages_of_one_signal_are_separate_rows(corpus):
    # VIP fires a bare market call, then sends levels ~40s later. Both are
    # real moments and both belong in the corpus.
    corpus.insert(snapshot(stage="market_call"))
    corpus.insert(snapshot(stage="levels"))

    assert corpus.counts()["pos"] == 2


def test_exists_reports_only_the_stage_that_was_written(corpus):
    corpus.insert(snapshot(stage="market_call"))

    assert corpus.exists("msg-1", "market_call") is True
    assert corpus.exists("msg-1", "levels") is False


# ── The resolver's queue ──────────────────────────────────────────────────────

def test_a_captured_signal_with_levels_is_offered_to_the_resolver(corpus):
    corpus.insert(snapshot())

    pending = corpus.unresolved()

    assert [p["tg_message_id"] for p in pending] == ["msg-1"]


def test_background_samples_are_never_offered_to_the_resolver(corpus):
    corpus.insert(snapshot(msg_id="bg-1", stage="background"))

    assert corpus.unresolved() == []


def test_a_signal_without_stated_levels_is_never_offered_to_the_resolver(corpus):
    # A bare market call states a direction and nothing else -- there is no
    # stop or target to judge it against.
    corpus.insert(snapshot(msg_id="msg-2", stage="market_call",
                           stop_loss=None, tp1=None))

    assert corpus.unresolved() == []


def test_a_signal_captured_moments_ago_is_held_back(corpus):
    # Nothing has happened yet; walking it forward would only ever say
    # "undecided" and burn a candle fetch doing it.
    corpus.insert(snapshot(msg_id="msg-3", signal_ts=time.time() - 10))

    assert corpus.unresolved() == []


def test_recording_an_outcome_removes_the_row_from_the_pending_queue(corpus):
    corpus.insert(snapshot())
    snap_id = corpus.unresolved()[0]["id"]

    corpus.set_outcome(snap_id, "win", 1.5)

    assert corpus.unresolved() == []
    counts = corpus.counts()
    assert counts["wins"] == 1
    assert counts["pending"] == 0


def test_a_resolved_outcome_is_readable_back_with_its_r_multiple(corpus):
    corpus.insert(snapshot())
    snap_id = corpus.unresolved()[0]["id"]

    corpus.set_outcome(snap_id, "loss", -1.0)

    row = corpus.rows(background=False)[0]
    assert row["outcome"] == "loss"
    assert row["outcome_r"] == -1.0


def test_the_cursor_survives_between_resolver_passes(corpus):
    corpus.insert(snapshot())
    snap_id = corpus.unresolved()[0]["id"]

    corpus.set_cursor(snap_id, 1_785_000_600.0, 4102.0)

    row = corpus.unresolved()[0]
    assert row["resolve_cursor_ts"] == 1_785_000_600.0
    assert row["entry_fill_price"] == 4102.0


def test_a_later_pass_does_not_wipe_a_fill_price_it_has_no_news_about(corpus):
    # resolve_pending passes None for the fill when a row is still undecided;
    # that must not erase the fill an earlier pass established.
    corpus.insert(snapshot())
    snap_id = corpus.unresolved()[0]["id"]
    corpus.set_cursor(snap_id, 1_785_000_600.0, 4102.0)

    corpus.set_cursor(snap_id, 1_785_004_200.0, None)

    assert corpus.unresolved()[0]["entry_fill_price"] == 4102.0
