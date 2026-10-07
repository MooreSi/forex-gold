"""Pure evidence fixtures; no broker or provider connections."""
import pytest
from backend.src.services.reversal_engine.evidence import observations as o


def signal():
    return {"signal_ref": "RE-A", "vantage_signal_id": "S1", "mt5_ticket": 123,
            "live_exec_status": "executed", "created_at": 10, "close_time": 60,
            "strategy": "template:fixed"}


def trade():
    return {"signal_id": "S1", "mt5_ticket": 123, "status": "closed", "trade_id": "T1",
            "initial_risk": 20, "net_pnl": 5, "mt5_profit": 5,
            "open_time": 20, "close_time": 60}


def test_broker_label_uses_total_net_once_and_actual_initial_risk():
    label = o.broker_label(signal(), trade(), 100)
    assert label["r"] == 0.25
    assert label["available_at"] == 100
    assert label["net"] == 5


@pytest.mark.parametrize("change", [{"initial_risk": 0}, {"mt5_ticket": 456},
    {"net_pnl": 6}, {"status": "open"}, {"mt5_profit": None}, {"initial_risk": float("nan")}])
def test_unreconciled_or_ambiguous_broker_label_is_refused(change):
    assert o.broker_label(signal(), {**trade(), **change}, 100) is None


def test_calendar_keeps_real_zero_surprise_and_arrival_time():
    event = o.calendar({"calendarId": "1", "date": "2026-10-07T12:00:00Z",
                        "actual": "0", "forecast": "0", "country": "United States",
                        "event": "CPI"}, 1791374500, "tradingeconomics")
    assert event["payload"]["surprise"] == 0
    assert event["available_at"] == 1791374500


def test_free_calendar_does_not_invent_actual_or_surprise():
    event = o.calendar({"title": "CPI", "country": "USD", "date": "2026-10-07T12:00:00Z",
                        "impact": "High", "forecast": "0.3%"}, 1791370000, "forexfactory")
    assert event["payload"]["surprise"] is None
    assert event["payload"]["currency"] == "USD"


def test_databento_integer_nanoprices_and_zero_imbalance():
    event = o.book({"hd": {"ts_event": 1791374400000000000, "instrument_id": 42},
        "levels": [{"bid_px": 4095000000000, "ask_px": 4095100000000,
                    "bid_sz": 10, "ask_sz": 10}], "sequence": 1}, 1791374401)
    assert event["payload"]["bid"] == 4095
    assert event["payload"]["spread"] == pytest.approx(0.1)
    assert event["payload"]["imbalance"] == 0


def test_crossed_book_is_refused():
    assert o.book({"hd": {"ts_event": 1791374400000000000, "instrument_id": 42},
        "levels": [{"bid_px": 4096000000000, "ask_px": 4095000000000,
                    "bid_sz": 10, "ask_sz": 10}]}, 1791374401) is None


def test_full_deal_net_includes_entry_commission_and_all_partial_exits():
    assert o.deal_net([{ "profit": 0, "swap": 0, "fee": 0, "commission": -.5 },
                       { "profit": 4, "swap": 0, "fee": 0, "commission": -.25 },
                       { "profit": 1, "swap": -.1, "fee": -.1, "commission": -.25 }]) == pytest.approx(3.8)


def test_missing_commission_is_unknown_not_zero():
    assert o.deal_net([{ "profit": 1, "swap": 0, "fee": 0 }]) is None


def test_partial_deal_history_cannot_be_treated_as_final_cashflows():
    assert o.deals_complete([{ "entry": 0, "volume": .02 }, { "entry": 1, "volume": .01 }]) is False


def test_full_partial_and_runner_history_is_complete():
    assert o.deals_complete([{ "entry": 0, "volume": .02 }, { "entry": 1, "volume": .01 },
                             { "entry": 1, "volume": .01 }]) is True


def test_finite_numbers_cannot_create_an_infinite_broker_r():
    assert o.broker_label(signal(), {**trade(), "initial_risk": 1e-320}, 100) is None


def test_overflowing_cashflows_are_not_a_training_label():
    assert o.deal_net([{ "profit": 1e308, "swap": 1e308, "fee": 0, "commission": 0 }]) is None
