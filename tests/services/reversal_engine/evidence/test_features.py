"""No feeds or broker; arrival-time leakage controls."""
from backend.src.services.reversal_engine.evidence.features import context


def test_revision_received_later_cannot_change_an_earlier_decision():
    events = [{"kind": "calendar", "key": "CPI", "source": "te", "event_ts": 10,
               "available_at": 20, "payload": {"currency": "USD", "scheduled_at": 10,
                                             "surprise": 0, "surprise_unit": "%"}},
              {"kind": "calendar", "key": "CPI", "source": "te", "event_ts": 10,
               "available_at": 40, "payload": {"currency": "USD", "scheduled_at": 10,
                                             "surprise": 2, "surprise_unit": "%"}}]
    assert context(events, 30)["release_surprise"] == 0
    assert context(events, 30)["release_missing"] == 0


def test_stale_futures_book_has_missing_flag_not_fake_zero_signal():
    events = [{"kind": "cme_book", "key": "42", "source": "databento",
               "event_ts": 10, "available_at": 11, "payload": {"imbalance": 0.8, "spread": 1}}]
    assert context(events, 1000)["cme_missing"] == 1


def test_fresh_book_is_visible():
    events = [{"kind": "cme_book", "key": "42", "source": "databento",
               "event_ts": 10, "available_at": 11, "payload": {"imbalance": 0.8, "spread": 1}}]
    assert context(events, 12)["cme_imbalance"] == 0.8


def test_latest_complete_calendar_snapshot_can_remove_a_cancelled_release():
    events = [{"kind": "calendar", "key": "CPI", "source": "forexfactory", "event_ts": 100,
               "available_at": 20, "payload": {"currency": "USD", "scheduled_at": 100,
                                               "impact": "high", "surprise": None}},
              {"kind": "calendar_batch", "key": "current_week", "source": "forexfactory",
               "event_ts": 40, "available_at": 40, "payload": {"keys": []}}]
    assert context(events, 30)["calendar_missing"] == 0
    assert context(events, 50)["calendar_missing"] == 1
