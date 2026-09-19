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
    """A KeyGen directory with a working forex_admin.py, found via $HOME, on a
    machine declared to be the licence issuer -- the console is pinned to
    hardware too since 2026-09-12, and this file exists precisely so the answer
    does not depend on which machine runs the suite."""
    from backend.src.config.licence import issuer as issuer_mod
    monkeypatch.delenv("FOREX_ADMIN_MACHINE_FINGERPRINT", raising=False)
    monkeypatch.setattr(issuer_mod, "_read_fingerprint",
                        lambda: issuer_mod.ADMIN_MACHINE_FINGERPRINT)

    home = tmp_path / "home"
    kg = home / "Documents" / "KeyGen"
    kg.mkdir(parents=True)
    (kg / "forex_admin.py").write_text(
        "def open_admin_dialog():\n    return 'opened'\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    # Patching $HOME is not enough. One candidate is `forex_root.parent /
    # "KeyGen"` -- derived from __file__, so on the owner's Mac it resolves to
    # the real ~/KeyGen no matter what $HOME says, and the lookup finds THAT
    # instead of this fixture's. Which one won then depended on whether an
    # earlier test had already put it on sys.path, so this file's results were
    # order-dependent and machine-dependent -- the exact thing its docstring
    # says it exists to stop. Pin the whole candidate list to tmp_path.
    monkeypatch.setattr(app_mod, "_admin_checkout_candidates",
                        lambda _root: [home / "forex-admin",
                                       home / "KeyGen",
                                       home / "Documents" / "KeyGen"])
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
    # See fake_keygen: $HOME alone leaves the real ~/KeyGen reachable through
    # the forex_root.parent candidate, and this assertion ("nothing is found")
    # then failed on the owner's machine while passing on CI.
    monkeypatch.setattr(app_mod, "_admin_checkout_candidates",
                        lambda _root: [home / "forex-admin",
                                       home / "KeyGen",
                                       home / "Documents" / "KeyGen"])
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
        monkeypatch.setattr(app_mod, "_admin_checkout_candidates",
                            lambda _root: [home / "forex-admin",
                                           home / "KeyGen",
                                           home / "Documents" / "KeyGen"])
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


# ── Which checkout wins (2026-09-19) ─────────────────────────────────────────

class TestTheTrackedCheckoutTakesPrecedence:
    """`~/forex-admin` is the git checkout (MooreSi/forex-admin). The two
    `KeyGen` folders are untracked copies that predate it, kept only so a
    machine that has not moved over still works.

    The order is the whole guarantee that the tracked copy is the one that
    runs -- and it decides more than which dialog opens, because the winner
    goes onto sys.path at position 0. A legacy folder winning also changes
    which `database.py` and `licence_signing.py` everything else resolves to,
    which is how the React console once read a different registry from the
    NiceGUI one on the same machine.
    """

    @pytest.fixture
    def three_checkouts(self, tmp_path, monkeypatch):
        from backend.src.config.licence import issuer as issuer_mod
        monkeypatch.delenv("FOREX_ADMIN_MACHINE_FINGERPRINT", raising=False)
        monkeypatch.setattr(issuer_mod, "_read_fingerprint",
                            lambda: issuer_mod.ADMIN_MACHINE_FINGERPRINT)

        home = tmp_path / "home"
        made = {}
        for label, relative in (("tracked", "forex-admin"),
                                ("sibling", "KeyGen"),
                                ("icloud",  "Documents/KeyGen")):
            path = home / relative
            path.mkdir(parents=True)
            (path / "forex_admin.py").write_text(
                f"def open_admin_dialog():\n    return {label!r}\n",
                encoding="utf-8")
            made[label] = path

        monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(app_mod, "_admin_checkout_candidates",
                            lambda _root: [made["tracked"], made["sibling"],
                                           made["icloud"]])
        sys.modules.pop("forex_admin", None)
        return made

    def test_forex_admin_wins_over_both_legacy_copies(self, three_checkouts,
                                                      monkeypatch):
        monkeypatch.syspath_prepend(str(three_checkouts["tracked"]))
        assert app_mod._find_admin_open_fn()() == "tracked"

    def test_the_legacy_copies_are_still_a_fallback(self, three_checkouts,
                                                    monkeypatch):
        """Removing them is a decision, not a refactor -- forex-admin's
        docs/simon-handover/002. Until then, a machine with only the old
        folder must keep working."""
        import shutil
        shutil.rmtree(three_checkouts["tracked"])
        monkeypatch.syspath_prepend(str(three_checkouts["sibling"]))
        assert app_mod._find_admin_open_fn()() == "sibling"

    def test_the_candidate_order_is_the_documented_one(self):
        """Against the real function, not the patched one -- the fixtures
        above replace it, so without this nothing checks what it returns."""
        candidates = app_mod._admin_checkout_candidates(Path("/srv/FOREX"))
        assert candidates == [
            Path.home() / "forex-admin",
            Path("/srv") / "KeyGen",
            Path.home() / "Documents" / "KeyGen",
        ]

    def test_guard_agrees_with_app(self):
        """guard.py keeps its own copy of the list. They only decide the same
        thing while they hold the same paths."""
        from pathlib import Path as _P
        source = (_P(__file__).resolve().parents[2]
                  / "backend/src/config/licence/guard.py").read_text(encoding="utf-8")
        assert '"forex-admin"' in source, "guard.py does not know about the tracked checkout"
