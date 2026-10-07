"""Feed fixtures, no network."""
from backend.src.services.reversal_engine.evidence.free_feeds import futures_bars


def test_only_closed_bars_are_recorded_with_actual_arrival_time():
    rows = [{"ts": 10, "close": 100, "volume": 10}, {"ts": 310, "close": 101, "volume": 20}]
    events = futures_bars(rows, 620)
    assert events[-1]["event_ts"] == 610
    assert events[-1]["available_at"] == 620
    assert events[-1]["payload"]["return"] == .01
    assert events[-1]["payload"]["delayed"] is True


def test_forming_bar_is_excluded():
    assert futures_bars([{"ts": 310, "close": 101, "volume": 20}], 400) == []


def test_calendar_uses_shared_lowercase_filters_and_preserves_currency(monkeypatch):
    from backend.src.utils import news_calendar
    from backend.src.services.reversal_engine.evidence import free_feeds
    def events(**kw):
        assert kw == {"currencies": {"USD", "XAU"}, "impacts": {"high", "medium", "low"}}
        return [{"ts": 100, "title": "CPI", "currency": "USD", "impact": "high"}]
    monkeypatch.setattr(news_calendar, "get_events", events)
    assert free_feeds.fetch_calendar()[0]["payload"]["currency"] == "USD"
