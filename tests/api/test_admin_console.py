"""Mounting the licence admin console.

The console lives in MooreSi/forex-admin, outside this repository, and is
discovered on disk at run time. Two things have to stay true:

1. **A missing or broken console never stops the app starting.** It is
   optional; the trading app is not. Every failure path returns False.
2. **It is never mounted on a machine that is not the licence issuer.** A
   checkout on disk is a filesystem fact, not an authorisation -- ~/Documents
   is iCloud-synced, so one lands on every Mac on the owner's Apple ID.

See forex-admin's docs/system/rules/70-wiring-into-the-apps.md.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI

from backend.src.api import admin_console


@pytest.fixture
def issuer(monkeypatch):
    """Declare this machine the licence issuer, whoever is running the suite."""
    from backend.src.config.licence import issuer as issuer_mod
    monkeypatch.delenv("FOREX_ADMIN_MACHINE_FINGERPRINT", raising=False)
    monkeypatch.setattr(issuer_mod, "_read_fingerprint",
                        lambda: issuer_mod.ADMIN_MACHINE_FINGERPRINT)


@pytest.fixture
def fake_console(tmp_path, monkeypatch, issuer):
    """A console checkout with a real, importable api package and a bundle.

    Written to disk rather than injected into `sys.modules`, because
    `admin_console` deliberately imports `<checkout>/api/__init__.py` by path
    -- a fixture that faked the module would skip the part most likely to
    break.
    """
    checkout = tmp_path / "forex-admin"
    (checkout / "api").mkdir(parents=True)
    (checkout / "api" / "routes.py").write_text("", encoding="utf-8")
    bundle = checkout / "web" / "dist"
    bundle.mkdir(parents=True)
    (bundle / "index.html").write_text("<html>console</html>", encoding="utf-8")

    (checkout / "api" / "__init__.py").write_text(
        "from pathlib import Path\n"
        "from fastapi import APIRouter\n"
        "\n"
        "REPO_ROOT = Path(__file__).resolve().parent.parent\n"
        "BUNDLE_DIR = REPO_ROOT / 'web' / 'dist'\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/api/admin/status')\n"
        "def _status() -> dict:\n"
        "    return {'available': True}\n"
        "\n"
        "def console_available() -> bool:\n"
        "    return True\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(admin_console, "CANDIDATES", (checkout,))
    monkeypatch.delitem(sys.modules, "api", raising=False)
    before = list(sys.path)
    yield checkout
    sys.path[:] = before
    sys.modules.pop("api", None)


class TestDiscovery:
    def test_the_tracked_checkout_leads(self):
        """~/forex-admin is the git checkout; the KeyGen folders are the
        untracked copies that predate it."""
        assert admin_console.CANDIDATES[0] == Path.home() / "forex-admin"
        assert len(admin_console.CANDIDATES) == 3

    def test_a_directory_is_only_a_console_if_it_has_the_api(self, tmp_path, monkeypatch):
        """Identified by api/routes.py, not by name. The legacy KeyGen copies
        have no api/ at all and must not be picked for the React console."""
        looks_right = tmp_path / "forex-admin"
        looks_right.mkdir()
        monkeypatch.setattr(admin_console, "CANDIDATES", (looks_right,))
        assert admin_console.find_checkout() is None

        (looks_right / "api").mkdir()
        (looks_right / "api" / "routes.py").write_text("", encoding="utf-8")
        assert admin_console.find_checkout() == looks_right

    def test_nothing_anywhere_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(admin_console, "CANDIDATES", (tmp_path / "nope",))
        assert admin_console.find_checkout() is None


class TestItNeverBreaksStartup:
    def test_a_non_issuer_machine_does_not_load_it(self, fake_console, monkeypatch):
        from backend.src.config.licence import issuer as issuer_mod
        monkeypatch.setattr(issuer_mod, "_read_fingerprint", lambda: "SOMEONE-ELSE")
        assert admin_console.load() is None

    def test_an_issuer_check_that_raises_is_survived(self, fake_console, monkeypatch):
        from backend.src.config.licence import issuer as issuer_mod

        def _boom():
            raise OSError("ioreg is not on this machine")
        monkeypatch.setattr(issuer_mod, "_read_fingerprint", _boom)
        assert admin_console.load() is None

    def test_an_import_error_is_survived(self, tmp_path, monkeypatch, issuer):
        checkout = tmp_path / "forex-admin"
        (checkout / "api").mkdir(parents=True)
        (checkout / "api" / "routes.py").write_text("", encoding="utf-8")
        (checkout / "api" / "__init__.py").write_text(
            "raise RuntimeError('licences.db is on a dead network mount')\n",
            encoding="utf-8")
        monkeypatch.setattr(admin_console, "CANDIDATES", (checkout,))
        monkeypatch.delitem(sys.modules, "api", raising=False)
        assert admin_console.load() is None

    def test_a_failed_import_leaves_sys_path_clean(self, tmp_path, monkeypatch, issuer):
        """Otherwise the broken checkout's top-level `api`, `core` and
        `database` shadow everything imported afterwards. Found by this
        file's own tests polluting each other."""
        checkout = tmp_path / "forex-admin"
        (checkout / "api").mkdir(parents=True)
        (checkout / "api" / "routes.py").write_text("", encoding="utf-8")
        (checkout / "api" / "__init__.py").write_text(
            "raise RuntimeError('boom')\n", encoding="utf-8")
        monkeypatch.setattr(admin_console, "CANDIDATES", (checkout,))
        monkeypatch.delitem(sys.modules, "api", raising=False)

        before = list(sys.path)
        assert admin_console.load() is None
        assert sys.path == before, "a failed checkout was left on sys.path"

    def test_a_console_missing_a_contract_name_is_refused(self, fake_console):
        """Better to show no button than a button wired to half an API."""
        (fake_console / "api" / "__init__.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n",          # no BUNDLE_DIR, no console_available
            encoding="utf-8",
        )
        sys.modules.pop("api", None)
        assert admin_console.load() is None

    def test_install_returns_false_rather_than_raising(self, tmp_path, monkeypatch):
        monkeypatch.setattr(admin_console, "CANDIDATES", (tmp_path / "nope",))
        app = FastAPI()
        assert admin_console.install(app) is False

    def test_an_api_with_no_built_bundle_is_not_mounted(self, fake_console):
        """The API would work and /admin would 404 -- a button that goes
        nowhere is worse than no button."""
        (fake_console / "web" / "dist" / "index.html").unlink()
        assert admin_console.install(FastAPI()) is False


class TestMounting:
    def test_it_mounts_the_api_and_the_bundle(self, fake_console):
        from fastapi.testclient import TestClient
        app = FastAPI()
        assert admin_console.install(app) is True

        client = TestClient(app)
        assert client.get("/api/admin/status").json() == {"available": True}
        assert client.get("/admin/").status_code == 200
        assert "console" in client.get("/admin/").text

    def test_the_checkout_is_appended_to_sys_path_not_prepended(self, fake_console):
        """It has top-level `api`, `core` and `database` modules. Inserting it
        at position 0 -- which backend/src/app.py does for the NiceGUI console
        -- lets those shadow anything this app imports by the same name."""
        before = list(sys.path)
        try:
            admin_console.load()
            assert str(fake_console) in sys.path
            assert sys.path[0] != str(fake_console)
        finally:
            sys.path[:] = before

    def test_the_real_console_mounts_when_one_is_installed(self, issuer):
        """The end-to-end check, on a machine that actually has the checkout.
        Skips elsewhere rather than passing vacuously."""
        if admin_console.find_checkout() is None:
            pytest.skip("no forex-admin checkout — the real mount is UNCHECKED")
        from fastapi.testclient import TestClient
        app = FastAPI()
        assert admin_console.install(app) is True
        body = TestClient(app).get("/api/admin/status").json()
        assert "can_sign" in body and "licence_count" in body
