"""news_pause_state — what the header's red NEWS box shows, and when.

The rule the owner asked for on 2026-09-04 is precise: the box appears when
trading HAS BEEN PAUSED because of a news event. Not when a news event exists,
and not when one is merely on the calendar.

The header badge that already existed keyed off get_current_event() alone,
which answers a different question -- "are we inside a calendar window" -- and
gets both directions wrong:

  * get_current_event() never consults the `enabled` flag (is_high_impact_
    window and check_news_blackout both do). With the blackout switched OFF
    the badge still appeared inside any USD high-impact window, announcing a
    pause that was not in force.
  * When the calendar feed is unreachable and nothing was ever cached,
    check_news_blackout falls back to a hardcoded schedule of the routine gold
    movers and DOES block entries -- while get_current_event returns None. So
    trading was paused for news with no indicator at all, which is exactly the
    case the box exists for.

check_news_blackout() is the same function the entry paths gate on, so keying
the badge to it makes "box visible" and "orders blocked for news" the same
fact by construction rather than by two implementations agreeing.
"""
import pytest

from backend.src.utils import news_calendar as nc


@pytest.fixture
def news(monkeypatch):
    """Drive news_pause_state's two inputs directly."""
    state = {"blackout": (True, ""), "event": None}

    monkeypatch.setattr(nc, "check_news_blackout", lambda: state["blackout"])
    monkeypatch.setattr(nc, "get_current_event", lambda *a, **k: state["event"])
    return state


def _event(**over):
    ev = {
        "title": "Non-Farm Employment Change",
        "currency": "USD",
        "impact": "high",
        "mins_remaining": 42.0,
        "window_end": 1_788_534_000.0,
        "event_ts": 1_788_532_200.0,
    }
    ev.update(over)
    return ev


def test_no_box_when_trading_is_not_paused_for_news(news):
    assert nc.news_pause_state()["paused"] is False


def test_a_blackout_with_a_known_event_shows_the_box(news):
    news["blackout"] = (False, "News blackout — Non-Farm Employment Change (USD), resumes in 42 min")
    news["event"] = _event()

    state = nc.news_pause_state()
    assert state["paused"] is True
    assert state["label"] == "NEWS"
    assert "Non-Farm Employment Change" in state["detail"]


def test_a_disabled_blackout_shows_nothing_even_inside_a_window(news):
    """The false positive. get_current_event ignores the enabled flag, so the
    old badge announced a pause that was not in force -- the operator reads
    that as 'orders are being held' when they are being placed."""
    news["blackout"] = (True, "")          # blackout off => trading allowed
    news["event"] = _event()               # ...but a window is open

    assert nc.news_pause_state()["paused"] is False


def test_the_fallback_blackout_shows_the_box_with_no_event_to_name(news):
    """The false negative, and the more dangerous one: the feed is down, the
    hardcoded schedule is holding orders, and nothing on screen says so."""
    news["blackout"] = (False, "News blackout — scheduled high-impact window (calendar unavailable)")
    news["event"] = None

    state = nc.news_pause_state()
    assert state["paused"] is True
    assert state["label"] == "NEWS"
    # it must still say something useful without an event to name
    assert state["detail"]


def test_the_detail_carries_the_resume_time_when_one_is_known(news):
    news["blackout"] = (False, "News blackout — CPI (USD), resumes in 12 min")
    news["event"] = _event(title="CPI m/m", mins_remaining=12.0)

    detail = nc.news_pause_state()["detail"]
    assert "12" in detail


def test_it_never_raises_when_the_calendar_misbehaves(monkeypatch):
    """This runs on the header's refresh timer. A calendar failure must cost
    the badge, not the header."""
    def _boom():
        raise RuntimeError("feed exploded")

    monkeypatch.setattr(nc, "check_news_blackout", _boom)
    state = nc.news_pause_state()
    assert state["paused"] is False


# ── The header's countdown (owner, 2026-09-04) ────────────────────────────────
#
# "if there is a news blackout window this needs to replace the 'Circuit Break
# OK' with 'News Blackout' and a timer". The badge is refreshed on a 5s timer
# and has no clock of its own, so the state has to carry a resume TIMESTAMP --
# a minutes figure computed once here would freeze on screen between refreshes
# and be wrong by up to the length of the window.


def test_the_resume_timestamp_is_carried_so_the_badge_can_count_down(news):
    news["blackout"] = (False, "News blackout — NFP (USD), resumes in 42 min")
    news["event"] = _event(window_end=1_788_534_000.0)

    assert nc.news_pause_state()["resume_ts"] == 1_788_534_000.0


def test_the_minutes_remaining_come_from_the_event(news):
    news["blackout"] = (False, "News blackout — CPI (USD), resumes in 12 min")
    news["event"] = _event(mins_remaining=12.0)

    assert nc.news_pause_state()["mins_remaining"] == 12.0


def test_a_fallback_blackout_has_no_resume_time_to_offer(news):
    """The hardcoded schedule knows a window is open, not when it ends. The
    badge must show the pause with no timer rather than invent one."""
    news["blackout"] = (False, "News blackout — scheduled high-impact window (calendar unavailable)")
    news["event"] = None

    state = nc.news_pause_state()
    assert state["paused"] is True
    assert state["resume_ts"] is None
    assert state["mins_remaining"] is None


def test_nothing_is_offered_when_trading_is_not_paused(news):
    state = nc.news_pause_state()

    assert (state["resume_ts"], state["mins_remaining"]) == (None, None)


def test_a_calendar_failure_still_answers_the_full_shape(monkeypatch):
    """The header reads every key each refresh; a missing one is a traceback
    on the timer, which takes the whole header with it."""
    def _boom():
        raise RuntimeError("feed exploded")

    monkeypatch.setattr(nc, "check_news_blackout", _boom)
    state = nc.news_pause_state()

    assert set(state) >= {"paused", "label", "detail", "resume_ts", "mins_remaining"}
