"""The KeyGen admin console must not appear on somebody else's client machine.

`_find_admin_open_fn()` decided the admin button on one fact: is there a
`KeyGen/forex_admin.py` next to the app or in `~/Documents`? Its own comment
says "Remote users don't have that directory" -- an assumption that stops being
true the moment `~/Documents` is synced (iCloud Drive) or the folder is copied
along with the app. Confirmed live 2026-09-06: a remote Mac on the LAN showed
the admin button while the console listed it as an ordinary client, so the
console's own "Remove Admin" could not take the button away -- it was never a
grant in the first place.

The grant is now the only route on a client machine. A machine that another
machine's admin server has welcomed as a client writes
`remote/is_remote_client` (see `remote/client.py`), and while that marker is
there the KeyGen path is refused no matter what is on disk;
`_find_remote_admin_open_fn()`'s explicit grant still works, which is what the
console's Grant Admin button is for.

The marker is deliberately NOT written on a machine that has its own admin
password -- the activation screen makes the admin Mac dial its own server
(`config/licence/guard.py`), and that welcome must not lock the owner out of
their own console.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from backend.src import app as app_mod
from backend.src import config as cfg_mod


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A machine with a KeyGen folder and its own (empty) user-data dir."""
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


class TestAMachineThatIsSomebodyElsesClient:
    def test_the_keygen_console_is_refused(self, machine):
        (machine / "is_remote_client").touch()

        assert app_mod._find_admin_open_fn() is None

    def test_an_explicit_grant_still_opens_the_remote_panel(self, machine):
        """Grant Admin on the console is the one supported route, and it has
        to keep working on exactly the machines the marker now covers."""
        (machine / "is_remote_client").touch()
        (machine / "is_remote_admin").touch()

        fn = app_mod._find_remote_admin_open_fn()

        assert fn is not None and fn() == "remote-opened"


class TestTheAdminMachineItself:
    def test_the_keygen_console_still_opens(self, machine):
        assert app_mod._find_admin_open_fn()() == "opened"

    def test_a_password_that_was_never_set_does_not_hide_the_button(self, machine):
        """Setting the admin password happens INSIDE the console
        (forex_admin.open_admin_dialog -> _show_setup_dialog), so gating the
        button on the password would make a fresh admin machine unbootstrappable.
        """
        assert not (machine / "admin_password.hash").exists()
        assert app_mod._find_admin_open_fn() is not None


class TestWhichAgentStarts:
    """A granted remote admin gets ADMIN_AVAILABLE too. Only the machine with
    the LOCAL KeyGen console may start the server; everything else dials it."""

    def test_the_server_starts_on_the_local_admin_machine(self, monkeypatch):
        monkeypatch.setattr(app_mod, "LOCAL_ADMIN_AVAILABLE", True)
        monkeypatch.setattr(app_mod, "password_is_set", lambda: True)

        assert app_mod._should_start_remote_server() is True

    def test_a_granted_remote_admin_does_not_start_a_server(self, monkeypatch):
        monkeypatch.setattr(app_mod, "LOCAL_ADMIN_AVAILABLE", False)
        monkeypatch.setattr(app_mod, "password_is_set", lambda: True)

        assert app_mod._should_start_remote_server() is False

    def test_no_password_means_no_server(self, monkeypatch):
        monkeypatch.setattr(app_mod, "LOCAL_ADMIN_AVAILABLE", True)
        monkeypatch.setattr(app_mod, "password_is_set", lambda: False)

        assert app_mod._should_start_remote_server() is False
