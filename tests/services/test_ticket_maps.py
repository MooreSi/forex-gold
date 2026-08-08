"""The ticket-map merge precedence, pinned.

These builders spent their life as private functions inside
`controllers/history/controller.py`, mixing three data sources with a
precedence rule that was never asserted anywhere. Moving them into
`services/analytics/ticket_maps.py` is what made them reachable by a test at
all -- so the move is the reason these assertions can exist.

Two behaviours matter and both are easy to break silently:

1. **Local rows win over the cross-node consolidated ledger.** A ticket this
   node opened has full local detail; the ledger carries only what the peer
   chose to publish. Reversing the order degrades every locally-opened trade
   to the peer's summary, and nothing would fail.

2. **One dead source must not empty the map.** A node with no peer has no
   ledger; a fresh install has no ladder-leg table. The `except Exception:
   pass` around each source is deliberate, and the thing it protects is that
   the *other* sources still land.
"""
from __future__ import annotations

import pytest

from backend.src.services.analytics import ticket_maps as tm


class _Boom:
    """Any attribute access raises -- stands in for a source that is absent."""
    def __getattr__(self, name):
        def _raise(*a, **k):
            raise RuntimeError(f"{name} unavailable")
        return _raise


@pytest.fixture
def sources(monkeypatch):
    """Patch both sources; each test sets what it needs."""
    state = {"ledger": {}, "repo": {}}

    class _Ledger:
        @staticmethod
        def get_consolidated_ticket_maps():
            return state["ledger"].get("maps", ({}, {}, {}))

        @staticmethod
        def get_consolidated_extra_maps():
            return state["ledger"].get("extra", ({}, {}))

    class _Repo:
        def __getattr__(self, name):
            return lambda *a, **k: state["repo"].get(name, [])

    monkeypatch.setattr(tm, "_ledger", _Ledger)
    monkeypatch.setattr(tm, "_repo", _Repo())
    return state


def test_local_rows_win_over_the_consolidated_ledger(sources):
    """The precedence rule, asserted directly rather than inferred."""
    sources["ledger"]["maps"] = ({"111": "PeerChannel"}, {}, {})
    sources["repo"]["ticket_sources"] = [(111, "LocalChannel")]

    result = tm._source_map(days=7)

    assert result["111"] == "LocalChannel", (
        "the local repo row must overwrite the ledger entry for the same "
        "ticket -- the ledger only carries what the peer published"
    )


def test_ledger_entries_survive_where_no_local_row_exists(sources):
    """Precedence is per-ticket, not whole-map replacement."""
    sources["ledger"]["maps"] = ({"111": "PeerOnly", "222": "PeerChannel"}, {}, {})
    sources["repo"]["ticket_sources"] = [(222, "LocalChannel")]

    result = tm._source_map(days=7)

    assert result == {"111": "PeerOnly", "222": "LocalChannel"}


def test_a_dead_ledger_does_not_empty_the_map(sources, monkeypatch):
    """A node with no peer configured still gets its own local tickets."""
    monkeypatch.setattr(tm, "_ledger", _Boom())
    sources["repo"]["ticket_sources"] = [(333, "LocalChannel")]

    result = tm._source_map(days=7)

    assert result == {"333": "LocalChannel"}


def test_a_dead_repo_does_not_discard_ledger_entries(sources, monkeypatch):
    monkeypatch.setattr(tm, "_repo", _Boom())
    sources["ledger"]["maps"] = ({"444": "PeerChannel"}, {}, {})

    result = tm._source_map(days=7)

    assert result == {"444": "PeerChannel"}


def test_ladder_legs_inherit_the_parent_trades_channel(sources):
    """Adaptive Runner legs 2+ get their own raw MT5 ticket and never get a
    simulated-trades row of their own, so they must pick the channel up from
    the leg query or the History table shows them as unattributed."""
    sources["repo"]["ticket_sources_for_legs"] = [(555, "ParentChannel")]

    result = tm._source_map(days=7)

    assert result["555"] == "ParentChannel"


def test_a_dpm_trade_is_labelled_dpm_regardless_of_its_strategy(sources):
    """dpm_trade_id present wins over the strategy column."""
    sources["repo"]["ticket_strategies"] = [(666, "scale_out", "dpm-abc")]

    result = tm._strategy_map(days=7)

    assert result["666"] == "DPM"


def test_a_non_dpm_trade_keeps_its_strategy_label(sources):
    sources["repo"]["ticket_strategies"] = [(777, "scale_out", None)]

    result = tm._strategy_map(days=7)

    assert result["777"] == "Scale Out"


def test_order_type_defaults_to_market_when_the_column_is_empty(sources):
    """order_type/pending_placed_at are not in the ledger sync protocol yet,
    so a NULL must read as a market order rather than blanking the column."""
    sources["repo"]["ticket_order_types"] = [(888, None, None)]

    result = tm._order_type_map(days=7)

    assert result["888"] == ("market", None)


def test_ticket_info_prefers_local_over_ledger_for_all_three_fields(sources):
    sources["ledger"]["maps"] = (
        {"999": "PeerChannel"}, {"999": "be_runner"}, {"999": "SELL"})
    sources["repo"]["all_ticket_info"] = [(999, "LocalChannel", "scale_out", "BUY")]

    result = tm._ticket_info()

    assert result["999"] == ("LocalChannel", "Scale Out", "BUY")


def test_extra_maps_merge_ledger_then_local(sources):
    sources["ledger"]["extra"] = ({"1": "TP3"}, {"1": 1.5})

    assert tm._max_tp_map()["1"] == "TP3"
    assert tm._rr_map()["1"] == 1.5
