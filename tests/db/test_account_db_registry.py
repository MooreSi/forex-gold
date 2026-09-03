"""A database per MT5 account, without losing the settings that are not the account's.

Owner, 2026-09-03: "if there is a fresh database for a new demo account all of
the risk settings, ea templates and credentials should remain, the account is
simply where the trades are executed".

Today the database is split by ENVIRONMENT only -- forex_trader_demo.db and
forex_trader_live.db -- so two demo accounts share one set of trades. Splitting
by login is the ask. The danger is everything else in that file: it also holds
22 EA templates, the risk settings, the Telegram config and the credentials.
A naive split gives a new account none of them, and it cannot even connect,
because the credentials live in the file it just left.

Two rules, and the first one matters more than the feature:

  1. **An existing install must keep its existing database.** If this resolver
     ever returns a new path for a login that is already using
     forex_trader_demo.db, the app opens an empty file and 1,309 trades look
     like they vanished. The registry claims the existing file for the first
     login it sees, precisely so that upgrading changes nothing.

  2. A genuinely new account gets a new trades file, seeded with the shared
     tables copied from the account it was created alongside.
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.src.db import account_registry as reg


@pytest.fixture
def data_dir(tmp_path):
    return tmp_path


def _make_db(path, *, trades=0):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE vantage_simulated_trades (trade_id TEXT)")
    conn.execute("CREATE TABLE vantage_risk_settings (id INTEGER, risk REAL)")
    conn.execute("CREATE TABLE ea_trade_templates (name TEXT)")
    conn.execute("CREATE TABLE mt5_credentials (id INTEGER, login TEXT)")
    conn.executemany("INSERT INTO vantage_simulated_trades VALUES (?)",
                     [(f"t{i}",) for i in range(trades)])
    conn.execute("INSERT INTO vantage_risk_settings VALUES (1, 2.5)")
    conn.executemany("INSERT INTO ea_trade_templates VALUES (?)",
                     [("GD VIP - Single",), ("Auto Limit Balanced",)])
    conn.execute("INSERT INTO mt5_credentials VALUES (1, '25470480')")
    conn.commit()
    conn.close()


class TestAnExistingInstallIsNotDisturbed:
    """The rule that must never break."""

    def test_the_first_login_claims_the_existing_file(self, data_dir):
        existing = data_dir / "forex_trader_demo.db"
        _make_db(existing, trades=1309)

        path = reg.resolve_db_path(data_dir, "demo", "25470480")

        assert path == existing

    def test_and_its_trades_are_still_there(self, data_dir):
        existing = data_dir / "forex_trader_demo.db"
        _make_db(existing, trades=1309)

        path = reg.resolve_db_path(data_dir, "demo", "25470480")
        conn = sqlite3.connect(path)
        n = conn.execute("SELECT COUNT(*) FROM vantage_simulated_trades").fetchone()[0]
        conn.close()

        assert n == 1309

    def test_the_same_login_keeps_resolving_to_the_same_file(self, data_dir):
        _make_db(data_dir / "forex_trader_demo.db", trades=5)

        first  = reg.resolve_db_path(data_dir, "demo", "25470480")
        second = reg.resolve_db_path(data_dir, "demo", "25470480")

        assert first == second

    def test_an_unknown_login_does_not_steal_a_claimed_file(self, data_dir):
        _make_db(data_dir / "forex_trader_demo.db", trades=5)
        reg.resolve_db_path(data_dir, "demo", "25470480")

        other = reg.resolve_db_path(data_dir, "demo", "99999999")

        assert other != data_dir / "forex_trader_demo.db"


class TestWithNoLoginAtAll:
    """First run, or credentials not entered yet. Must not invent a file."""

    def test_it_falls_back_to_the_environment_default(self, data_dir):
        assert reg.resolve_db_path(data_dir, "demo", "") == \
            data_dir / "forex_trader_demo.db"

    def test_none_behaves_the_same(self, data_dir):
        assert reg.resolve_db_path(data_dir, "demo", None) == \
            data_dir / "forex_trader_demo.db"

    def test_it_records_nothing(self, data_dir):
        """It must not claim the default file for an empty login.

        If it did, the first REAL login would find the environment already
        claimed and be handed a new empty database -- the exact failure this
        whole module exists to prevent. Found by mutation testing: deleting
        the early return still returned the right path, so only checking the
        registry catches it.
        """
        reg.resolve_db_path(data_dir, "demo", "")

        assert not (data_dir / reg.REGISTRY_NAME).exists()

    def test_and_a_real_login_afterwards_still_claims_the_default(self, data_dir):
        _make_db(data_dir / "forex_trader_demo.db", trades=1309)

        reg.resolve_db_path(data_dir, "demo", "")
        path = reg.resolve_db_path(data_dir, "demo", "25470480")

        assert path == data_dir / "forex_trader_demo.db"


class TestANewAccount:
    def test_it_gets_its_own_file(self, data_dir):
        _make_db(data_dir / "forex_trader_demo.db", trades=5)
        reg.resolve_db_path(data_dir, "demo", "25470480")

        new = reg.resolve_db_path(data_dir, "demo", "99999999")

        assert new.name == "forex_trader_demo_99999999.db"

    def test_live_and_demo_are_separate_even_for_one_login(self, data_dir):
        _make_db(data_dir / "forex_trader_demo.db", trades=1)
        _make_db(data_dir / "forex_trader_live.db", trades=1)

        d = reg.resolve_db_path(data_dir, "demo", "25470480")
        l = reg.resolve_db_path(data_dir, "live", "25470480")

        assert d != l


class TestTheSharedTablesTravel:
    """The whole point: a new account is where trades happen, nothing else."""

    def _seeded(self, data_dir):
        source = data_dir / "forex_trader_demo.db"
        _make_db(source, trades=1309)
        reg.resolve_db_path(data_dir, "demo", "25470480")
        new = reg.resolve_db_path(data_dir, "demo", "99999999")
        return sqlite3.connect(new)

    def test_the_ea_templates_come_across(self, data_dir):
        conn = self._seeded(data_dir)
        n = conn.execute("SELECT COUNT(*) FROM ea_trade_templates").fetchone()[0]
        conn.close()

        assert n == 2

    def test_the_risk_settings_come_across(self, data_dir):
        conn = self._seeded(data_dir)
        row = conn.execute("SELECT risk FROM vantage_risk_settings").fetchone()
        conn.close()

        assert row[0] == 2.5

    def test_the_credentials_come_across(self, data_dir):
        """Without these the new account cannot connect to MT5 at all."""
        conn = self._seeded(data_dir)
        row = conn.execute("SELECT login FROM mt5_credentials").fetchone()
        conn.close()

        assert row[0] == "25470480"

    def test_the_TRADES_do_NOT_come_across(self, data_dir):
        """The one thing that must not travel. A new account showing another
        account's 1,309 trades is worse than showing none."""
        conn = self._seeded(data_dir)
        n = conn.execute("SELECT COUNT(*) FROM vantage_simulated_trades").fetchone()[0]
        conn.close()

        assert n == 0


class TestItNeverRaises:
    def test_an_unreadable_registry_falls_back(self, data_dir, monkeypatch):
        """This decides which database the app opens. A crash here is a dead
        app; the environment default is always a safe answer."""
        (data_dir / "accounts.json").write_text("{ not json", encoding="utf-8")

        assert reg.resolve_db_path(data_dir, "demo", "25470480") == \
            data_dir / "forex_trader_demo.db"

    def test_a_crash_anywhere_falls_back_to_the_default(self, data_dir,
                                                        monkeypatch):
        """The outer guard. _load already swallows bad JSON on its own, so
        only an unexpected failure exercises this -- and it is the difference
        between a dead app and a working one on the default database."""
        def _boom(*a, **kw):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(reg, "_load", _boom)

        assert reg.resolve_db_path(data_dir, "demo", "25470480") == \
            data_dir / "forex_trader_demo.db"

    def test_a_failed_seed_still_returns_a_path(self, data_dir, monkeypatch):
        """Better an empty new database than no database."""
        _make_db(data_dir / "forex_trader_demo.db", trades=1)
        reg.resolve_db_path(data_dir, "demo", "25470480")

        def _boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(reg, "_seed_shared_tables", _boom)

        path = reg.resolve_db_path(data_dir, "demo", "99999999")

        assert path.name == "forex_trader_demo_99999999.db"
