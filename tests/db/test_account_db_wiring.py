"""run.py opens the database the registry names for the active account.

The registry itself is tested in test_account_db_registry.py. This is the
wiring: that startup actually consults it, reads the login for the ACTIVE
environment, and never lets a failure there stop the app opening a database.

The login lives in `mt5_credentials`, which is read from the master demo
database regardless of environment -- that predates this change and is why a
new account can be resolved at all before it has its own file.
"""
from __future__ import annotations

import pathlib

import pytest

from backend.src.db import account_registry as reg

REPO = pathlib.Path(__file__).resolve().parents[2]


def _code(rel: str) -> str:
    text = (REPO / rel).read_text(encoding="utf-8")
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.strip().startswith("#"))


class TestStartupUsesTheRegistry:
    def test_run_py_resolves_the_path(self):
        code = _code("run.py")

        assert "account_registry" in code
        assert "resolve_db_path" in code

    def test_it_is_resolved_before_the_database_is_opened(self):
        """Opening the default first and switching later would leave every
        table the licence screen reads pointing at the wrong file."""
        code = _code("run.py")

        assert code.index("resolve_db_path") < code.index("_db_mod.init(")


class TestTheLoginForTheActiveEnvironment:
    @pytest.mark.parametrize("env,field,expected", [
        ("demo", "login", "25470480"),
        ("live", "live_login", "77770000"),
    ])
    def test_the_right_field_is_read(self, env, field, expected):
        """demo and live keep their logins in different columns; reading the
        wrong one would point a live account at the demo database."""
        creds = {"login": "25470480", "live_login": "77770000"}

        assert reg.login_for_env(creds, env) == expected

    def test_a_missing_login_is_empty_not_none(self, ):
        assert reg.login_for_env({}, "demo") == ""

    def test_a_numeric_login_is_returned_as_text(self):
        """It becomes part of a filename and a registry key."""
        assert reg.login_for_env({"login": 25470480}, "demo") == "25470480"

    def test_a_zero_login_counts_as_unset(self):
        """The credentials table stores 0 for "not configured", and a
        forex_trader_demo_0.db would be a real file for a fake account."""
        assert reg.login_for_env({"login": 0}, "demo") == ""

    def test_whitespace_is_stripped(self):
        assert reg.login_for_env({"login": "  25470480 "}, "demo") == "25470480"


class TestItCannotStopTheAppStarting:
    def test_unreadable_credentials_yield_no_login(self, monkeypatch):
        """No login means the environment default, which is exactly what an
        install that has not entered credentials should get."""
        assert reg.login_for_env(None, "demo") == ""
