"""Whether the app asks for a password on restart is a setting.

Owner, 2026-09-02: "add the option to either having to login when the app
restarts using a password or not, make it selectable as either auto login or
with a password".

The default is DELIBERATELY "require a password". This app places live orders
with real money, and the gate is the only thing between someone at the
keyboard and the trading controls -- so an install that has never touched the
setting keeps asking, and turning it off has to be a decision somebody made on
purpose.

The gate reads `backend.src.config.get`, which serves from an in-memory dict
after the first load, so a per-request check costs nothing.
"""
from __future__ import annotations

import pytest

from frontend import auth_gate


class _Store(dict):
    """Stands in for app.storage.user."""


@pytest.fixture
def gate(monkeypatch):
    """The middleware's decision, isolated from NiceGUI's request context."""
    store = _Store()
    monkeypatch.setattr(auth_gate.app, "storage",
                        type("S", (), {"user": store})())
    return store


def _decide(path: str) -> str:
    """Return "allowed" or "redirected" for an unauthenticated request."""
    return "allowed" if auth_gate._may_pass(path) else "redirected"


class TestTheDefaultIsToAskForAPassword:
    def test_an_unset_install_still_requires_login(self, monkeypatch, gate):
        """Secure by default: never having opened Settings must not leave the
        trading controls open to anyone at the keyboard."""
        monkeypatch.setattr(auth_gate, "_auto_login_enabled", lambda: False)

        assert _decide("/") == "redirected"

    def test_the_config_default_is_password_required(self, monkeypatch):
        """Read through the real helper, with the key absent."""
        monkeypatch.setattr(auth_gate._cfg, "get_config",
                            lambda key, default=None: default)

        assert auth_gate._auto_login_enabled() is False


    def test_an_unreadable_config_keeps_the_door_shut(self, monkeypatch):
        """Fail closed. If the setting cannot be read -- a corrupt config, a
        permissions problem -- the safe answer is to ask for the password, not
        to wave everyone through. Mutation testing found this branch
        untested."""
        def _boom(key, default=None):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(auth_gate._cfg, "get_config", _boom)

        assert auth_gate._auto_login_enabled() is False

    def test_an_unreadable_config_still_redirects(self, monkeypatch, gate):
        """The same thing asserted through the gate, not just the helper."""
        def _boom(key, default=None):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(auth_gate._cfg, "get_config", _boom)

        assert _decide("/") == "redirected"


class TestWhenAutoLoginIsChosen:
    def test_the_gate_lets_a_fresh_session_through(self, monkeypatch, gate):
        monkeypatch.setattr(auth_gate, "_auto_login_enabled", lambda: True)

        assert _decide("/") == "allowed"

    def test_a_deep_link_is_allowed_too(self, monkeypatch, gate):
        monkeypatch.setattr(auth_gate, "_auto_login_enabled", lambda: True)

        assert _decide("/some/page") == "allowed"


class TestTheOpenPathsAreUnaffected:
    @pytest.mark.parametrize("path", ["/login", "/_nicegui/x", "/static/y",
                                      "/favicon.ico"])
    def test_they_pass_with_the_password_required(self, monkeypatch, gate, path):
        """The login page itself has to render, or the gate locks everyone
        out including the person trying to log in."""
        monkeypatch.setattr(auth_gate, "_auto_login_enabled", lambda: False)

        assert _decide(path) == "allowed"


class TestAnAuthenticatedSessionAlwaysPasses:
    def test_even_with_the_password_required(self, monkeypatch, gate):
        monkeypatch.setattr(auth_gate, "_auto_login_enabled", lambda: False)
        gate["authenticated"] = True

        assert _decide("/") == "allowed"


class TestTheSettingIsOffered:
    def test_the_security_section_exists(self):
        from frontend.pages.settings import _security

        assert hasattr(_security, "_render_security")

    def test_the_settings_page_renders_it(self):
        import pathlib

        src = pathlib.Path(
            "frontend/pages/settings/__init__.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

        assert "_render_security" in code
        assert "Security" in code
