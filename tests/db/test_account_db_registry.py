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


def _make_db(path, *, trades=0, peak_balance=None):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE vantage_simulated_trades (trade_id TEXT)")
    conn.execute("CREATE TABLE app_config (key TEXT PRIMARY KEY, value TEXT)")
    if peak_balance is not None:
        conn.execute("INSERT INTO app_config VALUES ('peak_balance', ?)",
                     (str(peak_balance),))
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


class TestThePeakBalanceWatermarkIsAccountScoped:
    """bugs/042 — the drawdown watermark followed the account split.

    `peak_balance` is monotonic and nothing lowers it. It lives in
    `app_config`, which the seed copies as an install-wide table, so account
    26004592's database opened holding **$2,403.25** from 25470480 — against a
    balance under $1,000, a 63% drawdown that would halt trading on the first
    close the moment the Risk Governor was switched on.

    The watermark describes what ONE ACCOUNT reached. It is re-anchored
    whenever the database is opened for an account it was not measured on, and
    `close_trade._update_peak_balance` sets it again from the live balance on
    the next close.
    """

    def _resolve_second_account(self, data_dir, peak_balance):
        source = data_dir / "forex_trader_demo.db"
        _make_db(source, trades=1309, peak_balance=peak_balance)
        reg.resolve_db_path(data_dir, "demo", "25470480")
        return reg.resolve_db_path(data_dir, "demo", "99999999")

    def _peak(self, path):
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT value FROM app_config WHERE key='peak_balance'").fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def test_the_watermark_does_not_follow_a_new_account(self, data_dir):
        new = self._resolve_second_account(data_dir, 2403.25)

        assert self._peak(new) is None

    def test_an_inherited_watermark_on_an_existing_file_is_re_anchored(self, data_dir):
        """Simon's install, exactly: the file already exists and already holds
        another account's watermark, so there is nothing left to seed."""
        existing = data_dir / "forex_trader_demo_26004592.db"
        _make_db(existing, peak_balance=2403.25)
        (data_dir / "accounts.json").write_text(
            '{"demo": {"25470480": "forex_trader_demo.db",'
            ' "26004592": "forex_trader_demo_26004592.db"}}', encoding="utf-8")
        _make_db(data_dir / "forex_trader_demo.db", peak_balance=2403.25)

        resolved = reg.resolve_db_path(data_dir, "demo", "26004592")

        assert resolved == existing
        assert self._peak(existing) is None

    def test_the_account_is_stamped_so_the_clear_happens_once(self, data_dir):
        """The clear is a one-time heal, not a boot-time reset.

        An unstamped database predates the stamp, so its watermark cannot be
        attributed and the first resolve clears it — that is the case above.
        What must not happen is a clear on every boot: the watermark would
        never survive long enough to measure a drawdown from. The stamp is what
        stops it, and `test_a_watermark_set_after_the_stamp_survives` is the
        test that pins the consequence."""
        existing = data_dir / "forex_trader_demo.db"
        _make_db(existing, peak_balance=2403.25)

        reg.resolve_db_path(data_dir, "demo", "25470480")

        conn = sqlite3.connect(existing)
        owner = conn.execute(
            "SELECT value FROM app_config WHERE key='peak_balance_account'").fetchone()
        conn.close()

        assert owner[0] == "25470480"

    def test_a_watermark_set_after_the_stamp_survives(self, data_dir):
        """The re-anchor must not fight `_update_peak_balance`: a watermark
        this account earned itself is still there on the next boot."""
        existing = data_dir / "forex_trader_demo.db"
        _make_db(existing, peak_balance=2403.25)
        reg.resolve_db_path(data_dir, "demo", "25470480")

        conn = sqlite3.connect(existing)
        conn.execute("INSERT OR REPLACE INTO app_config VALUES ('peak_balance', '1100.0')")
        conn.commit()
        conn.close()

        reg.resolve_db_path(data_dir, "demo", "25470480")

        assert self._peak(existing) == "1100.0"

    def test_a_database_with_no_app_config_still_resolves(self, data_dir):
        """Never raises. This decides which database the app opens."""
        existing = data_dir / "forex_trader_demo.db"
        conn = sqlite3.connect(existing)
        conn.execute("CREATE TABLE vantage_simulated_trades (trade_id TEXT)")
        conn.commit()
        conn.close()

        assert reg.resolve_db_path(data_dir, "demo", "25470480") == existing

    def test_no_login_stamps_nothing(self, data_dir):
        """With no login there is no account to attribute a watermark to, and
        the no-login path is documented as recording nothing."""
        existing = data_dir / "forex_trader_demo.db"
        _make_db(existing, peak_balance=2403.25)

        reg.resolve_db_path(data_dir, "demo", "")

        assert self._peak(existing) == "2403.25"

    def test_a_second_account_pointed_at_the_same_file_re_anchors(self, data_dir):
        """The stamp names an account, not "some account": a file stamped for
        25470480 does not keep its watermark when opened for 26004592."""
        path = data_dir / "forex_trader_demo.db"
        _make_db(path, peak_balance=2403.25)

        reg.reanchor_account_scoped_config(path, "25470480")
        conn = sqlite3.connect(path)
        conn.execute("INSERT OR REPLACE INTO app_config VALUES ('peak_balance', '2403.25')")
        conn.commit()
        conn.close()

        reg.reanchor_account_scoped_config(path, "26004592")

        assert self._peak(path) is None

    def test_an_unopenable_file_leaves_the_watermark_alone(self, data_dir):
        """Never raises: this runs on the path that decides which database the
        app opens, and a crash here is a dead app."""
        not_a_database = data_dir / "forex_trader_demo.db"
        not_a_database.mkdir()

        resolved = reg.resolve_db_path(data_dir, "demo", "25470480")

        assert resolved == not_a_database
        assert not_a_database.is_dir()

    def test_an_app_config_it_cannot_read_leaves_the_watermark_alone(self, data_dir):
        """An app_config of an unexpected shape must not take the app down, and
        must not half-clear anything."""
        path = data_dir / "forex_trader_demo.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE app_config (key TEXT)")   # no value column
        conn.execute("INSERT INTO app_config VALUES ('peak_balance')")
        conn.commit()
        conn.close()

        reg.reanchor_account_scoped_config(path, "25470480")

        conn = sqlite3.connect(path)
        rows = conn.execute("SELECT key FROM app_config").fetchall()
        conn.close()
        assert rows == [("peak_balance",)]

    def test_a_connection_that_refuses_to_close_does_not_take_the_app_down(
            self, data_dir, monkeypatch):
        """The module's promise is "never raises", and the close is the one
        step that runs after the work is already done."""
        path = data_dir / "forex_trader_demo.db"
        _make_db(path, peak_balance=2403.25)

        real_connect = sqlite3.connect

        class _RefusesToClose:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def __enter__(self):
                return self._conn.__enter__()

            def __exit__(self, *a):
                return self._conn.__exit__(*a)

            def close(self):
                self._conn.close()
                raise sqlite3.OperationalError("cannot close database")

        monkeypatch.setattr(reg.sqlite3, "connect",
                            lambda p, *a, **k: _RefusesToClose(real_connect(p, *a, **k)))

        reg.reanchor_account_scoped_config(path, "25470480")

        monkeypatch.undo()
        assert self._peak(path) is None          # the work still landed
