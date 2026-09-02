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


class _FakeKeyring:
    """An in-memory stand-in for the OS keychain."""

    def __init__(self):
        self.store: dict = {}

    def get_password(self, service, account):
        return self.store.get((service, account))

    def set_password(self, service, account, value):
        self.store[(service, account)] = value


@pytest.fixture
def fake_keychain(monkeypatch):
    """Stop the suite from reading and WRITING the developer's real keychain.

    `_load_or_create_key` tries the OS keychain FIRST and only falls back to a
    key file. Redirecting the key file therefore isolated nothing on macOS or
    Windows: the real keychain answered, and the tmp file was never even
    created (verified 2026-09-02 — the file did not exist after the call).

    Two consequences, one of which is worse than a dirty test:

      * on a machine that already has the key, the suite silently reads the
        owner's real credentials key;
      * on a machine that does NOT — a fresh laptop, a CI runner — the suite
        GENERATES a key and writes it into that user's real keychain.

    Found when a redirected HOME made macOS unable to locate a keychain at all
    and it put up a "Keychain Not Found — Reset To Defaults" dialog mid-run.
    A test suite should never be able to produce that prompt.
    """
    import sys

    monkeypatch.setitem(sys.modules, "keyring", _FakeKeyring())
    return sys.modules["keyring"]


@pytest.fixture
def real_crypto(monkeypatch, tmp_path, fake_keychain):
    """Real Fernet, with its key in a fake keychain and its file in tmp_path."""
    monkeypatch.setattr(sec, "_key_file_path", lambda: tmp_path / "secret.key")
    sec._get_fernet.cache_clear() if hasattr(sec._get_fernet, "cache_clear") else None
    monkeypatch.setattr(sec, "_fernet", None)
    return sec


class TestTheSuiteNeverTouchesTheRealKeychain:
    """A guard on the fixture above, not on production code."""

    def test_the_key_comes_from_the_fake_keychain(self, real_crypto,
                                                  fake_keychain):
        key = sec._load_or_create_key()

        assert fake_keychain.store, "nothing was stored in the fake keychain"
        assert key.decode() in [v for v in fake_keychain.store.values()]

    def test_a_second_call_reuses_it_rather_than_generating_a_new_one(
            self, real_crypto, fake_keychain):
        """A regenerated key cannot decrypt what the first one encrypted."""
        first = sec._load_or_create_key()

        assert sec._load_or_create_key() == first

    def test_the_file_fallback_still_works_when_there_is_no_keychain(
            self, monkeypatch, tmp_path):
        """The path a Linux box with no Secret Service actually takes."""
        import sys

        class _Broken:
            def get_password(self, *a):
                raise RuntimeError("no keychain here")

            def set_password(self, *a):
                raise RuntimeError("no keychain here")

        monkeypatch.setitem(sys.modules, "keyring", _Broken())
        monkeypatch.setattr(sec, "_key_file_path", lambda: tmp_path / "secret.key")

        key = sec._load_or_create_key()

        assert (tmp_path / "secret.key").exists()
        assert (tmp_path / "secret.key").read_bytes().strip() == key


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

    @pytest.mark.skipif(
        os.name == "nt",
        reason="POSIX file modes. Windows ignores chmod's permission bits -- "
               "os.chmod only toggles the read-only flag there -- so the file "
               "lands 0o666 and no amount of chmod will change it. Skipped "
               "rather than weakened: asserting a mode Windows never applied "
               "would be a green tick for a check that did not happen. The "
               "exposure is real and is recorded in the broker domain file; "
               "test_the_windows_exposure_is_written_down below keeps that "
               "record honest.",
    )
    def test_it_is_written_owner_read_write_only(self, creds_db, real_crypto,
                                                 bridge_file):
        assert cr.sync_bridge_credentials_file("demo") is True

        mode = stat.S_IMODE(os.stat(bridge_file).st_mode)
        assert mode == 0o600, f"credentials file is mode {mode:o}, not 0600"

    def test_the_windows_exposure_is_written_down(self):
        """Runs on every platform, including the one with the hole.

        A skipped test leaves no trace on the platform it skipped, so the gap
        would be invisible exactly where it exists. This pins the written
        record instead. Found 2026-09-02 from a Windows CI run that had been
        failing on the assertion above since 2026-09-01.
        """
        import pathlib as _pl

        doc = _pl.Path("docs/system/domains/broker/README.md").read_text(
            encoding="utf-8")

        assert "0o666" in doc, (
            "the Windows credentials-file exposure is no longer described in "
            "docs/system/domains/broker/README.md -- either restore it, or if "
            "it has genuinely been fixed, delete the skipif above with it")

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
