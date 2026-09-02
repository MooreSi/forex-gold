"""Every file this app writes a secret into goes through one restriction call.

The point is not that each site sets some permission -- they all did, with
`chmod(0o600)`, and on Windows that protects nothing. The point is that there
is exactly ONE place that knows how to restrict a file per platform, and that
every secret-writing site uses it. A fifth secret written with a bare chmod is
a hole that looks like a fix.

Structural where it has to be (the CA key writer is not easily driven without
generating a real key) and behavioural everywhere else.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

_SECRET_WRITERS = {
    "backend/src/services/broker/credentials_repo.py":
        "the MT5 login and plaintext broker password",
    "backend/src/services/cluster/remote/ca.py":
        "the private CA key -- anyone holding it can mint a trusted cert",
    "backend/src/config/secrets.py":
        "the key that decrypts the stored credentials",
    "backend/src/config/licence/store.py":
        "the licence",
}


class TestNoSecretIsWrittenWithABareChmod:
    @pytest.mark.parametrize("path,what", list(_SECRET_WRITERS.items()))
    def test_it_calls_the_shared_helper(self, path, what):
        src = pathlib.Path(path).read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

        assert "restrict_to_owner" in code, (
            f"{path} writes {what} and does not restrict it through "
            "backend.src.utils.file_perms.restrict_to_owner")

    @pytest.mark.parametrize("path,what", list(_SECRET_WRITERS.items()))
    def test_it_no_longer_relies_on_chmod_alone(self, path, what):
        """`os.chmod(p, 0o600)` outside the helper is the exact pattern that
        was silently a no-op on Windows."""
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") == "chmod"):
                raise AssertionError(
                    f"{path}:{node.lineno} still calls chmod directly; it "
                    f"protects {what} and does nothing on Windows")


class TestTheHelperIsActuallyCalled:
    def test_the_bridge_credentials_file_is_restricted(self, tmp_path,
                                                       monkeypatch):
        from backend.src.services.broker import credentials_repo as cr
        from backend.src.utils import file_perms

        seen: list = []
        monkeypatch.setattr(file_perms, "restrict_to_owner",
                            lambda p: seen.append(str(p)) or True)
        monkeypatch.setattr(cr, "_bridge_creds_path",
                            lambda: str(tmp_path / "bridge.json"))
        monkeypatch.setattr(cr, "get_mt5_credentials",
                            lambda: {"login": "1", "server": "s",
                                     "password_enc": "p"})

        cr.sync_bridge_credentials_file("demo")

        assert seen, "the credentials file was written without being restricted"

    def test_the_licence_store_is_restricted(self, tmp_path, monkeypatch):
        from backend.src.config.licence import store
        from backend.src.utils import file_perms

        seen: list = []
        monkeypatch.setattr(file_perms, "restrict_to_owner",
                            lambda p: seen.append(str(p)) or True)
        monkeypatch.setattr(store, "STORE_PATH", tmp_path / "licence.json")

        store.save({"licence_key": "k"})

        assert seen
