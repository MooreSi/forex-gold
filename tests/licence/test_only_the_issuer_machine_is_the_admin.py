"""Only the machine that issues licences may be the admin server.

Reported live 2026-09-12. A client MacBook came back from a restart running the
admin SERVER instead of the client: `app.startup()` is
`if _should_start_remote_server(): ... elif _remote_client_enabled(config): ...`,
so the promotion also stopped it dialling home, and the owner's console showed
it permanently offline while it sat on the LAN listening on 8443 with its own
`remote/tls.py`-shaped certificate.

`_should_start_remote_server()` was `LOCAL_ADMIN_AVAILABLE and
password_is_set()` -- two filesystem facts, both true on that machine by
accident: `~/Documents/KeyGen/forex_admin.py` arrives on every Mac signed into
the owner's Apple ID (`~/Documents` is inside the iCloud Drive container), and
a stale `remote/admin_password.hash` was left over from an earlier session.

The 2026-09-06 `is_remote_client` marker does not cover this. That marker is
written on MSG_WELCOME -- by the client loop the promotion prevents from
running -- so a machine promoted before its first welcome can never write it,
and one whose marker is missing for any other reason promotes again.

The issuer is therefore pinned to hardware. It adds no fragility that licensing
does not already carry: `get_fingerprint()` is what the licence itself is keyed
to, so a machine whose fingerprint drifts has lost its licence either way.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from backend.src import app as app_mod
from backend.src import config as cfg_mod
from backend.src.config.licence import issuer as issuer_mod


OTHER_MACHINE = "FOREX-11111111-22222222-33333333-44444444"


@pytest.fixture
def keygen_machine(tmp_path, monkeypatch):
    """A machine with KeyGen on disk -- the accident, not the authorisation."""
    home = tmp_path / "home"
    kg = home / "Documents" / "KeyGen"
    kg.mkdir(parents=True)
    (kg / "forex_admin.py").write_text(
        "def open_admin_dialog():\n    return 'opened'\n", encoding="utf-8")
    (kg / "admin_panel.py").write_text(
        "def open_dialog():\n    return 'remote-opened'\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    for mod in ("forex_admin", "admin_panel"):
        sys.modules.pop(mod, None)
    monkeypatch.syspath_prepend(str(kg))

    user_data = tmp_path / "userdata"
    (user_data / "remote").mkdir(parents=True)
    monkeypatch.setattr(cfg_mod, "USER_DATA_DIR", user_data)
    return user_data / "remote"


def _machine_is(monkeypatch, fingerprint: str) -> None:
    monkeypatch.delenv("FOREX_ADMIN_MACHINE_FINGERPRINT", raising=False)
    monkeypatch.setattr(issuer_mod, "_read_fingerprint", lambda: fingerprint)


class TestAClientMachineThatHappensToHaveKeyGen:
    def test_the_local_console_is_refused(self, keygen_machine, monkeypatch):
        _machine_is(monkeypatch, OTHER_MACHINE)

        assert app_mod._find_admin_open_fn() is None

    def test_a_stale_admin_password_cannot_promote_it_to_a_server(
        self, keygen_machine, monkeypatch,
    ):
        """The exact 2026-09-12 state: KeyGen present, password hash present.

        LOCAL_ADMIN_AVAILABLE is `_find_admin_open_fn() is not None`, so
        refusing the console is what keeps `_should_start_remote_server()`
        False and lets the client loop run.
        """
        _machine_is(monkeypatch, OTHER_MACHINE)
        (keygen_machine / "admin_password.hash").write_text("salt:digest", encoding="utf-8")

        local_admin_available = app_mod._find_admin_open_fn() is not None
        monkeypatch.setattr(app_mod, "LOCAL_ADMIN_AVAILABLE", local_admin_available)

        assert app_mod._should_start_remote_server() is False

    def test_an_explicit_grant_still_opens_the_remote_panel(
        self, keygen_machine, monkeypatch,
    ):
        """Grant Admin on the owner's console stays the one supported route to
        a console on a client machine -- the pin must not take that away."""
        _machine_is(monkeypatch, OTHER_MACHINE)
        (keygen_machine / "is_remote_admin").touch()

        fn = app_mod._find_remote_admin_open_fn()

        assert fn is not None and fn() == "remote-opened"


class TestTheIssuerMachine:
    def test_it_keeps_its_console(self, keygen_machine, monkeypatch):
        _machine_is(monkeypatch, issuer_mod.ADMIN_MACHINE_FINGERPRINT)

        assert app_mod._find_admin_open_fn()() == "opened"

    def test_the_pin_is_the_owners_own_machine(self):
        """A wrong constant here locks the owner out of their own console and
        takes the whole fleet's admin server down with it, so it is asserted
        rather than left to a comment."""
        assert issuer_mod.ADMIN_MACHINE_FINGERPRINT == (
            "FOREX-349E9267-EBB61E27-539D9A41-3AAC333E"
        )


class TestRecoveringFromAHardwareChange:
    def test_an_env_override_moves_the_pin(self, keygen_machine, monkeypatch):
        """The escape hatch for a replaced or reimaged admin Mac: without one,
        the only way back is editing a constant and shipping a build."""
        monkeypatch.setattr(issuer_mod, "_read_fingerprint", lambda: OTHER_MACHINE)
        monkeypatch.setenv("FOREX_ADMIN_MACHINE_FINGERPRINT", OTHER_MACHINE)

        assert app_mod._find_admin_open_fn()() == "opened"

    def test_the_override_is_not_a_blank_cheque(self, keygen_machine, monkeypatch):
        monkeypatch.setattr(issuer_mod, "_read_fingerprint",
                            lambda: "FOREX-99999999-99999999-99999999-99999999")
        monkeypatch.setenv("FOREX_ADMIN_MACHINE_FINGERPRINT", OTHER_MACHINE)

        assert app_mod._find_admin_open_fn() is None
