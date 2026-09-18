"""The header: what is on screen on every tab, and the one thing it must say.

**A halt must be visible.** Two independent things stop automated entries —
the risk governor and the circuit breaker — and they are stored under different
keys. Between 2026-09-18 and the fix this endpoint reported only the governor's,
so a tripped breaker appeared nowhere but Settings > Diagnostics: the header
said nothing at all while entries were being refused.

The other rule here predates the port. **The demo/live distinction must be
unmistakable**, which a field cannot be if it is absent — so a bridge that
cannot answer returns None and the badge renders "unknown", never "demo".

Nothing here reaches a broker: the engine is the suite's sentinel.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import system as system_router


@pytest.fixture
def header(monkeypatch, sentinel_engine):
    state = {
        "pause": {"paused": False, "reason": "", "until": None, "source": ""},
        "active_trader": "local",
        "connected": True,
    }
    monkeypatch.setattr(system_router.trading_ctl, "trading_pause_status",
                        lambda: state["pause"])
    monkeypatch.setattr(system_router.settings_ctl, "get_active_trader",
                        lambda: state["active_trader"])
    monkeypatch.setattr(system_router.sync_ctl, "is_connected",
                        lambda: state["connected"])
    monkeypatch.setattr(system_router.broker_ctl, "get_effective_ea_status",
                        lambda: (True, "global"))
    monkeypatch.setattr(system_router.broker_ctl, "ea_build_status",
                        lambda: (False, ""))
    monkeypatch.setattr(system_router.broker_ctl, "ea_badge_state",
                        lambda *a: ("green", "EA", "the EA is healthy"))
    return state


class TestAHaltIsVisible:
    def test_a_quiet_account_reports_no_halt(self, make_client, header):
        body = make_client().get("/api/system/header").json()

        assert body["pause"]["paused"] is False

    def test_a_governor_halt_is_carried_with_its_reason(self, make_client, header):
        header["pause"] = {"paused": True, "reason": "daily loss limit reached",
                           "until": 1_800_000_000.0, "source": "governor"}

        body = make_client().get("/api/system/header").json()

        assert body["pause"]["paused"] is True
        assert body["pause"]["reason"] == "daily loss limit reached"

    def test_a_tripped_circuit_breaker_reaches_the_header_too(
        self, make_client, header,
    ):
        """The gap. The breaker writes a different key from the governor, and
        a header that read only the governor said nothing while automated
        entries were being refused."""
        header["pause"] = {"paused": True,
                           "reason": "circuit breaker: 3 consecutive losing trades",
                           "until": 1_800_000_000.0, "source": "circuit-breaker"}

        body = make_client().get("/api/system/header").json()

        assert body["pause"]["source"] == "circuit-breaker"
        assert "circuit breaker" in body["pause"]["reason"]

    def test_the_resume_time_survives_the_round_trip(self, make_client, header):
        """"Halted" with no resume time leaves the operator watching a screen
        to find out when it lifts."""
        header["pause"] = {"paused": True, "reason": "drawdown",
                           "until": 1_800_000_000.0, "source": "governor"}

        assert make_client().get("/api/system/header").json()["pause"]["until"] == \
            pytest.approx(1_800_000_000.0)


class TestTheAccountIsNeverGuessedAt:
    def test_the_account_is_carried_through(self, make_client, header, sentinel_engine):
        sentinel_engine.account = {"login": 5203117, "is_demo": True}

        body = make_client().get("/api/system/header").json()

        assert body["account"]["is_demo"] is True

    def test_a_bridge_that_cannot_answer_returns_none_not_a_default(
        self, make_client, header, sentinel_engine,
    ):
        """The UI must make demo/live unmistakable, which it cannot do from an
        absent field — so this says None and the badge says "unknown". A
        default of "demo" on a live account is the worst answer available."""
        sentinel_engine.account = None

        assert make_client().get("/api/system/header").json()["account"] is None


class TestTheEaBadgeIsDecidedByTheService:
    def test_its_colour_and_words_come_from_the_backend(self, make_client, header):
        """Re-deriving the rule in TypeScript would recreate the 2026-09-09 bug
        — a stale EA build showing green — in a second language."""
        badge = make_client().get("/api/system/header").json()["ea_badge"]

        assert badge["colour"] == "green"
        assert badge["text"] == "EA"
        assert badge["tooltip"] == "the EA is healthy"


def test_the_header_places_nothing(make_client, header, sentinel_engine):
    """Negative control for the whole file: it is a read, on every tab, every
    few seconds."""
    make_client().get("/api/system/header")

    assert [c for c in sentinel_engine.calls
            if c[0] in ("open_manual_market_order", "close_trade")] == []
