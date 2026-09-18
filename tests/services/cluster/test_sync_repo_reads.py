"""Cross-node reads and the centralized-signal-generation gate.

Same story as the analytics repos: these were executed by NiceGUI pages and
asserted by nothing. The page is gone, so the coverage went with it and the
gap became visible.

Two of the behaviours here decide whether a node *acts*, which is why they are
worth more than their line count:

* `should_generate_signals_here()` is what stops the VPS paying for a second
  Claude analysis of a signal the Mac has already analysed. Getting it wrong in
  one direction doubles the AI spend; in the other it silences a node that is
  supposed to be generating.
* `generate_sync_token()` / `get_sync_token()` are what pair two nodes. The
  token is persisted encrypted, so a round trip that silently returned the
  ciphertext would pair nothing and read as "the other node is offline".

Nothing here can reach a broker or a network: the sync server module is
replaced with a stand-in, and the only I/O is `fresh_db`'s temporary SQLite
file.
"""
from __future__ import annotations

import time

import pytest

from backend.src.db import database as db_module
from backend.src.services.cluster import node as node_facade
from backend.src.services.cluster import sync_repo as repo


def _ledger_row(**over) -> None:
    """One consolidated_trades row — a trade another node published to us."""
    row = {
        "node_id": "vps-1", "trade_id": "t-1", "engine": "breakout",
        "direction": "BUY", "strategy": "scale_out", "received_at": time.time(),
        "mt5_ticket": None, "max_tp_hit": None, "rr": None, "tg_source": None,
    }
    row.update(over)
    repo._ensure_sync_tables()
    with db_module.db() as conn:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT INTO consolidated_trades ({cols}) VALUES ({marks})",
            tuple(row.values()),
        )


# ── The consolidated ledger's later-arriving columns ─────────────────────────

class TestConsolidatedExtraMaps:
    """"rr at close, max_tp_hit 30+ min after" — the two populate at different
    times, which is why they are read separately from the main maps and why a
    row can legitimately carry one and not the other."""

    def test_a_row_with_both_columns_appears_in_both_maps(self, fresh_db):
        _ledger_row(mt5_ticket=123456, max_tp_hit="TP3", rr=2.4)

        max_tp, rr = repo.get_consolidated_extra_maps()

        assert max_tp == {"123456": "TP3"}
        assert rr == {"123456": 2.4}

    def test_a_row_that_has_only_one_of_them_appears_in_only_one_map(self, fresh_db):
        """The half-populated row is the normal case for the 30 minutes
        between close and the max-TP sweep."""
        _ledger_row(mt5_ticket=123456, max_tp_hit=None, rr=1.8)

        max_tp, rr = repo.get_consolidated_extra_maps()

        assert max_tp == {}
        assert rr == {"123456": 1.8}

    def test_an_rr_of_zero_is_kept_because_zero_is_a_measurement(self, fresh_db):
        """`if rr is not None`, not `if rr`. A trade that closed exactly at
        entry has an R of 0 — dropping it would show "not measured yet" for a
        trade that was measured."""
        _ledger_row(mt5_ticket=123456, rr=0.0)

        _max_tp, rr = repo.get_consolidated_extra_maps()

        assert rr == {"123456": 0.0}

    def test_a_row_with_no_ticket_is_not_in_the_maps(self, fresh_db):
        """The maps are keyed by broker ticket; a row without one cannot be
        matched to anything on screen."""
        _ledger_row(mt5_ticket=None, max_tp_hit="TP1", rr=1.0)

        assert repo.get_consolidated_extra_maps() == ({}, {})

    def test_an_empty_ledger_returns_two_empty_maps(self, fresh_db):
        assert repo.get_consolidated_extra_maps() == ({}, {})


# ── Who generates signals ────────────────────────────────────────────────────

class TestShouldGenerateSignalsHere:
    """The paid-AI gate. Every branch, because each one is a different
    deployment and only one of them says no."""

    @pytest.fixture
    def centralised(self, fresh_db, monkeypatch):
        monkeypatch.setattr(
            repo, "get_risk_settings",
            lambda: {"centralized_signal_gen_enabled": True},
        )

    def _server(self, monkeypatch, instance):
        """Stand in for the sync server module. No socket is opened."""
        import backend.src.services.cluster.sync.server as srv

        monkeypatch.setattr(srv, "get_instance", lambda: instance)

    def test_centralisation_off_means_this_node_generates(self, fresh_db, monkeypatch):
        monkeypatch.setattr(
            repo, "get_risk_settings",
            lambda: {"centralized_signal_gen_enabled": False},
        )

        assert repo.should_generate_signals_here() is True

    def test_a_node_with_no_sync_server_is_the_mac_and_always_generates(
        self, centralised, monkeypatch,
    ):
        self._server(monkeypatch, None)

        assert repo.should_generate_signals_here() is True

    def test_the_vps_generates_while_the_mac_is_the_active_trader(
        self, centralised, monkeypatch,
    ):
        """Local mode. "VPS is standing down anyway — unaffected"."""
        self._server(monkeypatch, object())
        monkeypatch.setattr(repo, "get_active_trader", lambda: "local")

        assert repo.should_generate_signals_here() is True

    def test_the_vps_stops_generating_only_when_it_is_also_the_active_trader(
        self, centralised, monkeypatch,
    ):
        """The one False. All three conditions must hold: centralisation on,
        this node is physically the VPS, and Remote mode is active."""
        self._server(monkeypatch, object())
        monkeypatch.setattr(repo, "get_active_trader", lambda: "remote_vps")

        assert repo.should_generate_signals_here() is False


# ── The pairing token ────────────────────────────────────────────────────────

class TestSyncToken:
    def test_a_generated_token_reads_back_as_itself(self, fresh_db):
        """It is stored encrypted, so this is the round trip that matters — a
        getter returning ciphertext would pair nothing and look like a network
        problem."""
        token = repo.generate_sync_token()

        assert repo.get_sync_token() == token

    def test_the_stored_form_is_not_the_plaintext(self, fresh_db):
        """Negative control on the test above: without this, a `generate` that
        stored the token in the clear would pass just as happily."""
        token = repo.generate_sync_token()

        assert db_module.get_app_config("sync_token_enc") != token

    def test_a_node_that_has_never_paired_has_no_token(self, fresh_db):
        """Empty string, not None and not a crash — the settings panel renders
        this directly."""
        assert repo.get_sync_token() == ""

    def test_generating_again_replaces_the_previous_token(self, fresh_db):
        """"Overwrites any previous token — do this once per node pairing, not
        per restart." Keeping both would leave two tokens that both look valid."""
        first = repo.generate_sync_token()
        second = repo.generate_sync_token()

        assert second != first
        assert repo.get_sync_token() == second


# ── Instant-entry follow-up matching ─────────────────────────────────────────

class TestFindLatestInstantTrade:
    """The bounded lookup a forwarded follow-up message is matched against.
    "An unbounded copy on this side would swallow signals exactly as the local
    one did" — so the window is the point, not an optimisation."""

    @pytest.fixture(autouse=True)
    def timeout(self, monkeypatch):
        import backend.src.services.trading.instant_followup as ifu

        monkeypatch.setattr(ifu, "ime_timeout_secs", lambda: 3600)

    def _open_trade(self, trade_id, *, tg_source, open_time):
        with db_module.db() as conn:
            conn.execute(
                "INSERT INTO vantage_signals (signal_id, source_name, direction, "
                "entry_low, entry_high, stop_loss, created_at) "
                "VALUES (?, ?, 'BUY', 2430.7, 2431.7, 2421.2, ?)",
                (trade_id, tg_source, open_time),
            )
            conn.execute(
                "INSERT INTO vantage_simulated_trades "
                "(trade_id, signal_id, direction, entry_low, entry_high, "
                " entry_price, lot_size, remaining_lots, stop_loss, tp1, "
                " status, open_time, tg_source) "
                "VALUES (?, ?, 'BUY', 2430.7, 2431.7, 2431.2, 0.05, 0.05, "
                " 2421.2, 2440.0, 'open', ?, ?)",
                (trade_id, trade_id, open_time, tg_source),
            )

    def test_it_finds_an_open_trade_from_that_channel(self, fresh_db):
        self._open_trade("t-1", tg_source="GoldSignals", open_time=time.time() - 60)

        found = repo.find_latest_instant_trade("GoldSignals")

        assert found is not None
        assert found["trade_id"] == "t-1"

    def test_it_matches_the_legacy_instant_prefix_too(self, fresh_db):
        """Older rows stored the channel as "instant:<name>"."""
        self._open_trade("t-1", tg_source="instant:GoldSignals",
                         open_time=time.time() - 60)

        assert repo.find_latest_instant_trade("GoldSignals")["trade_id"] == "t-1"

    def test_a_trade_older_than_the_timeout_is_not_matched(self, fresh_db):
        """The bound. Without it a follow-up would be applied to a trade from
        days ago."""
        self._open_trade("t-1", tg_source="GoldSignals",
                         open_time=time.time() - 2 * 3600)

        assert repo.find_latest_instant_trade("GoldSignals") is None

    def test_another_channels_trade_is_not_matched(self, fresh_db):
        """Negative control on the channel filter."""
        self._open_trade("t-1", tg_source="OtherChannel", open_time=time.time() - 60)

        assert repo.find_latest_instant_trade("GoldSignals") is None

    def test_the_most_recent_of_several_wins(self, fresh_db):
        self._open_trade("older", tg_source="GoldSignals", open_time=time.time() - 600)
        self._open_trade("newer", tg_source="GoldSignals", open_time=time.time() - 60)

        assert repo.find_latest_instant_trade("GoldSignals")["trade_id"] == "newer"


# ── The node facade ──────────────────────────────────────────────────────────

class TestNodeFacade:
    """`cluster/node.py` exists to be the one surface over `sync_repo` that the
    app shell and the pages read. Its whole job is to forward unchanged, so
    that is what is asserted — a facade that quietly transformed a value would
    be a second answer to "which node is trading"."""

    @pytest.mark.parametrize("name", [
        "get_active_trader", "generate_sync_token", "get_sync_token",
    ])
    def test_it_returns_what_the_repo_returned(self, name, monkeypatch):
        monkeypatch.setattr(repo, name, lambda: "sentinel-value")

        assert getattr(node_facade, name)() == "sentinel-value"

    def test_setting_the_active_trader_passes_the_value_through(self, monkeypatch):
        seen = []
        monkeypatch.setattr(repo, "set_active_trader",
                            lambda value, *a, **k: seen.append((value, a, k)))

        node_facade.set_active_trader("remote_vps")

        assert seen == [("remote_vps", (), {})]

    def test_the_facade_exports_exactly_these_four_names(self):
        """A facade that grows silently stops being one. Adding a name here is
        a decision; this makes it a visible one."""
        assert node_facade.__all__ == [
            "get_active_trader", "set_active_trader",
            "generate_sync_token", "get_sync_token",
        ]
