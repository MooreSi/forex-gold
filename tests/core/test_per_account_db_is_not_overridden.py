"""The per-account database must survive app startup.

bugs/031. `run.py` resolves the right file for the logged-in account:

    _login   = _acct.login_for_env(get_mt5_credentials(), _env)
    _db_path = str(_acct.resolve_db_path(cfg_module.DATA_DIR, _env, _login))
    _db_mod.init(_db_path)

and then `backend/src/app.py` called `db_module.init(config["db_path"])` --
the ENVIRONMENT default -- which threw that away.

**Measured live on 2026-09-09.** The bridge was trading account 26004592
(balance $970.57) while the running app had `forex_trader_demo.db` open, which
`accounts.json` registers to 25470480 and whose recorded balance was
-$4,904.89. 182 trades from 2026-09-03 onward were booked to the wrong
account's ledger. Position sizing, the daily-loss halt and the
`equity_drawdown_pct` ML feature all read that wrong balance.

`resolve_db_path` was never the problem -- it returns
`forex_trader_demo_26004592.db` for that login, verified directly. The second
`init()` was.

One resolver, called from both places, so the two cannot disagree again.
"""
from __future__ import annotations

import inspect

from backend.src.db import account_registry as acct


class TestOneResolverForEveryone:
    def test_the_registry_exposes_a_single_entry_point(self):
        """Both callers ask the same function. Two call sites each doing their
        own `resolve_db_path(...)` dance is what produced this bug."""
        assert hasattr(acct, "db_path_for_env")

    def test_run_py_uses_it(self):
        src = open("run.py", encoding="utf-8").read()
        assert "db_path_for_env" in src

    def test_app_startup_uses_it_too(self):
        """The regression this file exists for: app.py must not re-init with
        the environment default and undo run.py's work."""
        from backend.src import app

        src = inspect.getsource(app)
        assert "db_path_for_env" in src, (
            "app.py resolves the database some other way — that is the bug"
        )

    def test_app_startup_does_not_init_from_the_raw_config_path(self):
        """`config["db_path"]` is the ENVIRONMENT default. Initialising from it
        points a multi-account install at the wrong account's file."""
        from backend.src import app

        src = inspect.getsource(app)
        assert 'init(config["db_path"])' not in src
        assert "init(config['db_path'])" not in src


class TestTheResolverItself:
    def test_the_FIRST_login_seen_claims_the_existing_default_file(self, tmp_path):
        """Deliberate, and it is what makes upgrading a no-op: an install that
        has always used `forex_trader_demo.db` must keep using it rather than
        wake up pointing at an empty file with its 1,500 trades apparently
        gone."""
        got = acct.db_path_for_env(tmp_path, "demo", {"login": 26004592})

        assert got.name == "forex_trader_demo.db"

    def test_a_SECOND_login_gets_its_own_file(self, tmp_path):
        """The live shape: 25470480 claimed the default, so 26004592 -- the
        account actually being traded on 2026-09-09 -- gets its own."""
        acct.db_path_for_env(tmp_path, "demo", {"login": 25470480})   # claims default

        got = acct.db_path_for_env(tmp_path, "demo", {"login": 26004592})

        assert got.name == "forex_trader_demo_26004592.db"

    def test_a_claim_is_remembered(self, tmp_path):
        """Asking twice must give the same answer, or a restart moves the
        account's ledger."""
        first = acct.db_path_for_env(tmp_path, "demo", {"login": 25470480})
        acct.db_path_for_env(tmp_path, "demo", {"login": 26004592})

        assert acct.db_path_for_env(tmp_path, "demo", {"login": 25470480}) == first

    def test_an_unreadable_login_falls_back_to_the_environment_default(self, tmp_path):
        """Never raise on the boot path: a missing login must still start the
        app, on the default file, exactly as before."""
        for creds in ({}, None, {"login": 0}, {"login": ""}):
            got = acct.db_path_for_env(tmp_path, "demo", creds)
            assert got.name == "forex_trader_demo.db", creds

    def test_live_and_demo_read_different_columns(self, tmp_path):
        """demo and live keep their logins in different columns of one row, so
        reading the wrong one would point a live account at the demo file."""
        creds = {"login": 26004592, "live_login": 29377272}
        # Another login claims each environment's default first, so both of
        # these get their own file and the names are distinguishable.
        acct.db_path_for_env(tmp_path, "demo", {"login": 25470480})
        acct.db_path_for_env(tmp_path, "live", {"live_login": 11111111})

        assert acct.db_path_for_env(tmp_path, "demo", creds).name == "forex_trader_demo_26004592.db"
        assert acct.db_path_for_env(tmp_path, "live", creds).name == "forex_trader_live_29377272.db"
