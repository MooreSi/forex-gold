"""Finding (or not finding) KeyGen's admin module, on a machine that has neither.

Why this file exists: `backend/src/app.py`'s admin discovery ran its success
path only on a machine with `~/Documents/KeyGen` actually installed — the
owner's Mac. Everywhere else, including every CI runner, it fell through to
"not found" and those lines never executed.

The consequence was not a bug, it was a **coverage floor nobody could reach**.
`backend/src/app.py` was baselined at 26.5% from a run on a machine with KeyGen
present; CI measured 21.5% and the ratchet failed, correctly, on a difference
that was entirely environmental. Measured 2026-09-02: 21% with KeyGen on the
path, 17% without, same tests.

The fix is not to move the floor. It is to stop the coverage depending on the
machine — so these tests build a KeyGen directory in a tmp_path and point the
lookup at it. Both branches now run everywhere.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from backend.src import app as app_mod


@pytest.fixture
def fake_keygen(tmp_path, monkeypatch):
    """A KeyGen directory with a working forex_admin.py, found via $HOME."""
    home = tmp_path / "home"
    kg = home / "Documents" / "KeyGen"
    kg.mkdir(parents=True)
    (kg / "forex_admin.py").write_text(
        "def open_admin_dialog():\n    return 'opened'\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setitem(sys.modules, "forex_admin", None)
    sys.modules.pop("forex_admin", None)
    monkeypatch.syspath_prepend(str(kg))
    return kg


@pytest.fixture
def no_keygen(tmp_path, monkeypatch):
    """A machine with no KeyGen anywhere — every CI runner, and any second
    developer's laptop."""
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


class TestWhenKeyGenIsInstalled:
    def test_the_admin_dialog_is_found(self, fake_keygen):
        fn = app_mod._find_admin_open_fn()

        assert fn is not None
        assert fn() == "opened"

    def test_the_keygen_directory_joins_sys_path(self, fake_keygen):
        """forex_admin.py imports its siblings (database.py, licence_signing.py)
        by bare name, so the directory has to be importable, not just readable."""
        app_mod._find_admin_open_fn()

        assert str(fake_keygen) in sys.path


class TestWhenItIsNot:
    def test_nothing_is_found_and_nothing_raises(self, no_keygen):
        """The common case, and the one that must never take startup down:
        no KeyGen simply means no admin button."""
        assert app_mod._find_admin_open_fn() is None


class TestWhenTheModuleIsBroken:
    def test_an_import_error_is_survived(self, tmp_path, monkeypatch):
        """A forex_admin.py that raises on import must hide the button, not
        stop the app booting. It runs real work at import time — it opens a
        database — so this is not hypothetical."""
        home = tmp_path / "home"
        kg = home / "Documents" / "KeyGen"
        kg.mkdir(parents=True)
        (kg / "forex_admin.py").write_text(
            "raise RuntimeError('licences.db is on a dead network mount')\n",
            encoding="utf-8")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        sys.modules.pop("forex_admin", None)
        monkeypatch.syspath_prepend(str(kg))

        assert app_mod._find_admin_open_fn() is None


class TestTheImportTimeout:
    def test_a_module_that_returns_is_returned(self, monkeypatch):
        assert app_mod._import_with_timeout("json") is not None

    def test_a_hanging_import_gives_up_rather_than_blocking_startup(self,
                                                                    monkeypatch):
        """The reason the timeout exists: iCloud evicted the owner's
        licences.db and forex_admin.py's import blocked for ever, so the app
        never started. It must return None instead of hanging."""
        import importlib

        def _hang(name):
            import time
            time.sleep(30)

        monkeypatch.setattr(importlib, "import_module", _hang)

        assert app_mod._import_with_timeout("whatever", timeout=0.2) is None
