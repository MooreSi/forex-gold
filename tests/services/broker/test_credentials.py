"""MT5 credentials: what is encrypted, what is written in the clear, and which
account they connect to.

Three separate concerns live in this module, and only the last one is obvious:

  * **At rest in the database** the passwords are encrypted. The plaintext must
    not appear in the file.
  * **In `bridge_credentials.json`** they are NOT encrypted — the bridge needs
    plaintext to call `mt5.initialize()`. The only thing protecting that file
    is its 0600 mode, so the chmod is load-bearing rather than tidy.
  * **Which columns are read** decides which ACCOUNT the bridge connects to.
    Reading the live columns while `env == "demo"` would point the bridge at
    the real account, and nothing downstream would know: every order after
    that is real money on a run everyone believes is a practice one.

Nothing here touches the real credential database or the real
`bridge_credentials.json` — every path is redirected into tmp_path.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat

import pytest

from backend.src.config import secrets as sec
from backend.src.services.broker import credentials_repo as cr

_ROW = {
    "id": 1,
    "login": 111111, "password_enc": "demo-pass", "server": "Demo-Server",
    "terminal_path": "/demo/terminal",
    "live_login": 999999, "live_password_enc": "LIVE-PASS",
    "live_server": "Live-Server", "live_terminal_path": "/live/terminal",
}


@pytest.fixture
def creds_db(monkeypatch, tmp_path):
    """A credentials database of our own, with plaintext passwords in it."""
    path = tmp_path / "creds.db"
    conn = sqlite3.connect(path)
    cols = ", ".join(f"{k} TEXT" for k in _ROW if k != "id")
    conn.execute(f"CREATE TABLE mt5_credentials (id INTEGER PRIMARY KEY, {cols})")
    conn.execute(
        f"INSERT INTO mt5_credentials ({', '.join(_ROW)}) "
        f"VALUES ({', '.join('?' * len(_ROW))})", list(_ROW.values()))
    conn.commit()
    conn.close()
    monkeypatch.setattr(cr, "_master_creds_path", lambda: str(path))
    return path


@pytest.fixture
def real_crypto(monkeypatch, tmp_path):
    """Real Fernet, with its key file in tmp_path rather than the install."""
    monkeypatch.setattr(sec, "_key_file_path", lambda: tmp_path / "secret.key")
    sec._get_fernet.cache_clear() if hasattr(sec._get_fernet, "cache_clear") else None
    return sec


@pytest.fixture
def bridge_file(monkeypatch, tmp_path):
    path = tmp_path / "bridge_credentials.json"
    monkeypatch.setattr(cr, "_bridge_creds_path", lambda: str(path))
    return path


class TestSecretsAreEncryptedAtRest:

    def test_the_plaintext_password_is_not_in_the_database_file(
            self, creds_db, real_crypto):
        cr.save_mt5_credentials({"password_enc": "hunter2-in-the-clear"})

        assert b"hunter2-in-the-clear" not in creds_db.read_bytes()

    def test_it_round_trips(self, creds_db, real_crypto):
        cr.save_mt5_credentials({"password_enc": "hunter2-in-the-clear"})

        assert cr.get_mt5_credentials()["password_enc"] == "hunter2-in-the-clear"

    def test_the_live_password_is_encrypted_too(self, creds_db, real_crypto):
        cr.save_mt5_credentials({"live_password_enc": "live-secret-value"})

        assert b"live-secret-value" not in creds_db.read_bytes()

    def test_non_secret_columns_are_left_alone(self, creds_db, real_crypto):
        """The server name is not a secret, and encrypting it would break every
        screen that displays it."""
        cr.save_mt5_credentials({"server": "SomeBroker-Live"})

        assert b"SomeBroker-Live" in creds_db.read_bytes()

    def test_a_legacy_plaintext_row_is_upgraded_on_first_read(
            self, creds_db, real_crypto):
        """The fixture writes plaintext, as an install predating encryption
        would have. Reading it must leave the file encrypted."""
        assert b"demo-pass" in creds_db.read_bytes()

        got = cr.get_mt5_credentials()

        assert got["password_enc"] == "demo-pass", "the upgrade lost the value"
        assert b"demo-pass" not in creds_db.read_bytes(), (
            "a plaintext password was read and left on disk in the clear"
        )


class TestTheBridgeFileIsPlaintextAndOwnerOnly:
    """The bridge needs plaintext to call mt5.initialize(). Its permissions are
    the only thing protecting it."""

    def test_it_is_written_owner_read_write_only(self, creds_db, real_crypto,
                                                 bridge_file):
        assert cr.sync_bridge_credentials_file("demo") is True

        mode = stat.S_IMODE(os.stat(bridge_file).st_mode)
        assert mode == 0o600, f"credentials file is mode {mode:o}, not 0600"

    def test_it_contains_the_decrypted_password(self, creds_db, real_crypto,
                                                bridge_file):
        cr.sync_bridge_credentials_file("demo")

        assert json.loads(bridge_file.read_text())["password"] == "demo-pass"


class TestWhichAccountTheBridgeIsPointedAt:
    """The one that decides whether an order is real."""

    def test_demo_writes_the_DEMO_login_and_server(self, creds_db, real_crypto,
                                                   bridge_file):
        assert cr.sync_bridge_credentials_file("demo") is True

        payload = json.loads(bridge_file.read_text())
        assert payload["login"] == 111111
        assert payload["server"] == "Demo-Server"
        assert payload["password"] == "demo-pass"

    def test_demo_never_writes_a_LIVE_value(self, creds_db, real_crypto,
                                            bridge_file):
        """Stated separately from the test above because this is the failure
        that costs money: a demo run pointed at the real account."""
        cr.sync_bridge_credentials_file("demo")

        blob = bridge_file.read_text()
        assert "999999" not in blob
        assert "Live-Server" not in blob
        assert "LIVE-PASS" not in blob

    def test_live_writes_the_live_values(self, creds_db, real_crypto,
                                         bridge_file):
        """Positive control: the live path must still work, or the guard above
        would be satisfied by a function that always wrote demo."""
        assert cr.sync_bridge_credentials_file("live") is True

        payload = json.loads(bridge_file.read_text())
        assert payload["login"] == 999999
        assert payload["server"] == "Live-Server"

    def test_the_terminal_path_follows_the_environment(self, creds_db,
                                                       real_crypto, bridge_file):
        """It tells the bridge which already-running terminal to attach to.
        The wrong one attaches to the wrong account's terminal."""
        cr.sync_bridge_credentials_file("live")
        assert json.loads(bridge_file.read_text())["terminal_path"] == "/live/terminal"

        cr.sync_bridge_credentials_file("demo")
        assert json.loads(bridge_file.read_text())["terminal_path"] == "/demo/terminal"


class TestAnIncompleteSetIsRefusedRatherThanHalfWritten:
    """A file with a login and no password makes the bridge fail in a way that
    looks like a broker problem."""

    @pytest.mark.parametrize("blank", ["login", "password_enc", "server"])
    def test_a_missing_field_writes_nothing(self, creds_db, real_crypto,
                                            bridge_file, blank):
        cr.save_mt5_credentials({blank: ""})

        assert cr.sync_bridge_credentials_file("demo") is False
        assert not bridge_file.exists()

    def test_an_existing_file_is_left_alone_when_the_set_is_incomplete(
            self, creds_db, real_crypto, bridge_file):
        """It must not truncate a working file on the way to refusing."""
        cr.sync_bridge_credentials_file("demo")
        before = bridge_file.read_text()

        cr.save_mt5_credentials({"server": ""})

        assert cr.sync_bridge_credentials_file("demo") is False
        assert bridge_file.read_text() == before


class TestReadingFailsSoftly:
    def test_a_missing_database_returns_empty_rather_than_raising(
            self, monkeypatch, tmp_path):
        """This runs at startup. Raising here stops the app before it can show
        the operator anything useful."""
        monkeypatch.setattr(cr, "_master_creds_path",
                            lambda: str(tmp_path / "nope.db"))

        assert cr.get_mt5_credentials() == {}
