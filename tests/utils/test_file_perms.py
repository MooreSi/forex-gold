"""Restricting a secret file to its owner, on Windows as well as POSIX.

Windows clients are in scope (owner, 2026-09-02), and `os.chmod` does not
restrict a file there -- it toggles a read-only flag and nothing else. Four
places in this app write a secret and "protect" it with `chmod(0o600)`:

  services/broker/credentials_repo.py  the MT5 login and PLAINTEXT password
  services/cluster/remote/ca.py        the private CA key
  config/secrets.py                    the key that decrypts the credentials
  config/licence/store.py              the licence

On Windows all four landed `0o666` -- readable by any local account. The CA key
is the worst of them: anyone holding it can mint a certificate the app trusts.

This module is the one place that knows how to do it per-platform, so a fifth
secret cannot be written with a chmod that silently does nothing.

The Windows branch is exercised two ways: simulated here (so the command is
pinned on any machine), and for real by `test_the_acl_is_actually_applied`,
which is skipped off Windows and runs on CI -- the only place this repo can
verify the real behaviour, since it is developed on a Mac.
"""
from __future__ import annotations

import os
import stat
import subprocess

import pytest

from backend.src.utils import file_perms


@pytest.fixture
def secret(tmp_path):
    p = tmp_path / "secret.key"
    p.write_text("sensitive", encoding="utf-8")
    return p


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
class TestOnPosix:
    def test_it_becomes_owner_read_write_only(self, secret):
        assert file_perms.restrict_to_owner(secret) is True

        assert stat.S_IMODE(secret.stat().st_mode) == 0o600

    def test_an_already_open_file_is_closed_down(self, secret):
        """The real starting state: json.dump created it under the umask."""
        os.chmod(secret, 0o666)

        file_perms.restrict_to_owner(secret)

        assert stat.S_IMODE(secret.stat().st_mode) & 0o077 == 0


class TestTheWindowsBranch:
    """Simulated, so the command is pinned from any machine."""

    @pytest.fixture
    def on_windows(self, monkeypatch):
        calls: list = []

        def _run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(file_perms, "_is_windows", lambda: True)
        monkeypatch.setattr(file_perms.subprocess, "run", _run)
        monkeypatch.setattr(file_perms, "_current_user", lambda: "TESTUSER")
        return calls

    def test_it_uses_icacls(self, secret, on_windows):
        assert file_perms.restrict_to_owner(secret) is True

        assert on_windows and on_windows[0][0] == "icacls"

    def test_inherited_permissions_are_removed(self, secret, on_windows):
        """Without this the file keeps the directory's ACEs -- which is exactly
        how it ends up readable by every local account."""
        file_perms.restrict_to_owner(secret)

        assert "/inheritance:r" in on_windows[0]

    def test_only_the_current_user_is_granted(self, secret, on_windows):
        file_perms.restrict_to_owner(secret)

        assert "/grant:r" in on_windows[0]
        assert any("TESTUSER" in part for part in on_windows[0])

    def test_the_path_is_passed(self, secret, on_windows):
        file_perms.restrict_to_owner(secret)

        assert str(secret) in on_windows[0]

    def test_a_failed_icacls_reports_false(self, secret, monkeypatch):
        """Returning True here would tell a caller the secret is protected when
        it is not. Silence is the failure mode this whole module exists to
        end."""
        monkeypatch.setattr(file_perms, "_is_windows", lambda: True)
        monkeypatch.setattr(file_perms, "_current_user", lambda: "TESTUSER")
        monkeypatch.setattr(
            file_perms.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "denied"))

        assert file_perms.restrict_to_owner(secret) is False

    def test_a_failure_is_logged_loudly(self, secret, monkeypatch, caplog):
        monkeypatch.setattr(file_perms, "_is_windows", lambda: True)
        monkeypatch.setattr(file_perms, "_current_user", lambda: "TESTUSER")
        monkeypatch.setattr(
            file_perms.subprocess, "run",
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "denied"))

        with caplog.at_level("WARNING"):
            file_perms.restrict_to_owner(secret)

        assert any(r.levelname in ("WARNING", "ERROR") for r in caplog.records)

    def test_an_unidentifiable_user_is_refused_not_guessed(self, secret,
                                                            monkeypatch):
        """`icacls path /grant:r :F` is not a restriction, it is a syntax
        error -- and reporting success for it would leave the secret open
        while telling the caller it is protected."""
        calls: list = []
        monkeypatch.setattr(file_perms, "_is_windows", lambda: True)
        monkeypatch.setattr(file_perms, "_current_user", lambda: "")
        monkeypatch.setattr(file_perms.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd))

        assert file_perms.restrict_to_owner(secret) is False
        assert calls == [], "ran icacls with no user to grant to"

    def test_a_missing_file_is_not_handed_to_icacls(self, tmp_path,
                                                    monkeypatch):
        """Restricting a file that was never written is a caller bug worth
        reporting, not a command worth running."""
        calls: list = []
        monkeypatch.setattr(file_perms, "_is_windows", lambda: True)
        monkeypatch.setattr(file_perms, "_current_user", lambda: "TESTUSER")
        monkeypatch.setattr(file_perms.subprocess, "run",
                            lambda cmd, **kw: calls.append(cmd))

        assert file_perms.restrict_to_owner(tmp_path / "never.key") is False
        assert calls == []

    def test_icacls_missing_is_survived(self, secret, monkeypatch):
        """A stripped-down Windows image without icacls must not crash a write
        of the credentials file."""
        monkeypatch.setattr(file_perms, "_is_windows", lambda: True)
        monkeypatch.setattr(file_perms, "_current_user", lambda: "TESTUSER")

        def _boom(cmd, **kw):
            raise FileNotFoundError("icacls")

        monkeypatch.setattr(file_perms.subprocess, "run", _boom)

        assert file_perms.restrict_to_owner(secret) is False


class TestItNeverRaises:
    def test_a_missing_file_reports_false(self, tmp_path):
        """Callers write-then-restrict. If the write failed, this must not turn
        one problem into a traceback on the trading path."""
        assert file_perms.restrict_to_owner(tmp_path / "nope.key") is False


@pytest.mark.skipif(os.name != "nt", reason="the real check, runs on Windows CI")
class TestOnWindowsForReal:
    def test_the_acl_is_actually_applied(self, secret):
        """The one test that proves it. Developed on a Mac, verified here."""
        assert file_perms.restrict_to_owner(secret) is True

        out = subprocess.run(["icacls", str(secret)], capture_output=True,
                             text=True).stdout
        user = file_perms._current_user()
        assert user.split("\\")[-1].lower() in out.lower()
        for group in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
            assert group.lower() not in out.lower(), out
