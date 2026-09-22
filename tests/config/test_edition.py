"""The build's two gates: the licence key, and the dashboard password.

Both are ON in the code that ships to a paying user and OFF in the open-source
build (owner, 2026-09-22 — the repo is being opened up for the community).
Nothing was deleted to do it: `config/licence/` and `api/auth.py` are intact
and still tested, and flipping either constant here puts the gate back.

This file is the switch's own test. It is deliberately about the switch and
not about the gates: what the guard does once it IS required is
`tests/licence/`, and what the login gate does once it IS installed is
`tests/api/test_auth_gate.py`. Neither changed.
"""
from __future__ import annotations

import importlib

import pytest

from backend.src.config import edition


@pytest.fixture(autouse=True)
def _no_inherited_env(monkeypatch):
    """The owner's own shell may set either override. A test that reads it is
    asserting something about whoever ran it."""
    monkeypatch.delenv(edition.LICENCE_ENV_VAR, raising=False)
    monkeypatch.delenv(edition.LOGIN_ENV_VAR, raising=False)


class TestTheOpenSourceDefault:
    def test_a_licence_key_is_not_required(self):
        assert edition.licence_required() is False

    def test_a_dashboard_password_is_not_required(self):
        assert edition.authentication_required() is False

    def test_the_constants_are_what_the_functions_report(self):
        """The one-line route back: set the constant, the gate returns. If the
        functions stopped reading them, re-enabling would silently do nothing.
        """
        assert edition.LICENCE_REQUIRED is False
        assert edition.AUTHENTICATION_REQUIRED is False


class TestFlippingTheConstantPutsTheGateBack:
    def test_setting_licence_required_requires_a_licence(self, monkeypatch):
        monkeypatch.setattr(edition, "LICENCE_REQUIRED", True)
        assert edition.licence_required() is True

    def test_setting_authentication_required_requires_a_password(self, monkeypatch):
        monkeypatch.setattr(edition, "AUTHENTICATION_REQUIRED", True)
        assert edition.authentication_required() is True


class TestThePerRunOverride:
    """An environment variable turns a gate on for one run, without editing a
    tracked file — so a private build can be checked out of this same repo."""

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_the_env_var_turns_the_licence_gate_on(self, monkeypatch, value):
        monkeypatch.setenv(edition.LICENCE_ENV_VAR, value)
        assert edition.licence_required() is True

    @pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
    def test_the_env_var_turns_the_login_gate_on(self, monkeypatch, value):
        monkeypatch.setenv(edition.LOGIN_ENV_VAR, value)
        assert edition.authentication_required() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_an_off_value_leaves_it_off(self, monkeypatch, value):
        monkeypatch.setenv(edition.LICENCE_ENV_VAR, value)
        monkeypatch.setenv(edition.LOGIN_ENV_VAR, value)
        assert edition.licence_required() is False
        assert edition.authentication_required() is False

    def test_the_env_var_cannot_turn_a_required_gate_OFF(self, monkeypatch):
        """One direction only. An env var that could disable a licence check
        would be the bypass this repo's rules forbid -- it would travel with
        any build, including one sold with the gate compiled in."""
        monkeypatch.setattr(edition, "LICENCE_REQUIRED", True)
        monkeypatch.setattr(edition, "AUTHENTICATION_REQUIRED", True)
        monkeypatch.setenv(edition.LICENCE_ENV_VAR, "0")
        monkeypatch.setenv(edition.LOGIN_ENV_VAR, "0")
        assert edition.licence_required() is True
        assert edition.authentication_required() is True


class TestItSitsAtTheBottomOfTheStack:
    def test_it_imports_nothing_from_the_app(self):
        """`config/` may depend on `utils/` and `config/` and nothing else
        (import_contracts: utils-and-config-depend-on-nothing-above-them).
        run.py reads this module before the database is open."""
        import pathlib
        src = pathlib.Path(edition.__file__).read_text(encoding="utf-8")
        for line in src.splitlines():
            if line.startswith(("import ", "from ")):
                assert "backend.src" not in line, line

    def test_importing_it_does_not_need_a_config_file(self):
        """It is read on the pre-boot path, before `config.load()`."""
        importlib.reload(edition)
