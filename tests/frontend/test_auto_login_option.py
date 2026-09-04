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


class TestTheChoiceSurvivesARestart:
    """The bug the owner hit, 2026-09-04: choosing "Log in automatically" and
    pressing Save appeared to work, and the radio was back on "Ask for the
    dashboard password" the next time the page was opened.

    Every other test in this file stubs `_auto_login_enabled` or `get_config`,
    so none of them ever ran the value through the real save-and-reload path --
    which is where it was being dropped. `config.load()` rebuilds its in-memory
    dict from a fixed list of known keys, so a key written to config.yaml that
    load() does not name is gone the moment save_to_yaml() reloads.
    """

    @pytest.fixture
    def isolated_config(self, monkeypatch, tmp_path):
        """A real config.yaml in a temp dir, with the module cache restored."""
        import backend.src.config as cfg_module

        before = dict(cfg_module._cfg)
        monkeypatch.setattr(cfg_module, "CONFIG_FILE", tmp_path / "config.yaml")
        yield cfg_module
        cfg_module._cfg = before

    def test_saving_auto_login_persists_it(self, isolated_config):
        """Through the same controller the Save button calls, and the same
        helper the gate reads."""
        from backend.src.controllers import settings_controller as controller

        controller.save_config({"auto_login_enabled": True})

        assert controller.get_config("auto_login_enabled", False) is True
        assert auth_gate._auto_login_enabled() is True

    def test_it_is_still_there_after_a_fresh_load(self, isolated_config):
        """A restart re-reads the file from scratch."""
        from backend.src.controllers import settings_controller as controller

        controller.save_config({"auto_login_enabled": True})
        isolated_config.load()

        assert auth_gate._auto_login_enabled() is True

    def test_turning_it_back_off_persists_too(self, isolated_config):
        """Negative control: the round trip carries False, not just True --
        a fix that hardcoded True would pass the two tests above."""
        from backend.src.controllers import settings_controller as controller

        controller.save_config({"auto_login_enabled": True})
        controller.save_config({"auto_login_enabled": False})

        assert auth_gate._auto_login_enabled() is False
