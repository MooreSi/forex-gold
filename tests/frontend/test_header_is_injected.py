"""The header is a view, so it is handed its backend access rather than importing it.

restructure phase1/060, the second of the two sites keeping the frontend
contract off zero. `frontend/app/_header.py` imported `backend.src.app`
directly for three things: the runtime handle (an open-trades read and a tick
read), and the admin dialog's availability flag plus its open callback.

The repo's convention is that a controller read takes the engine as an argument
(`trading_controller.get_open_trades(engine)`), so the caller has to hold a
handle from somewhere. Changing that convention is a bigger job than this lane.
The narrow fix is the one CLAUDE.md already prescribes for exactly this
situation: inject from the composition root, which is a sanctioned site, rather
than reaching for it from a view.

`frontend/app/__init__.py` keeps the import. `_header.py` receives what it
needs and imports nothing above itself.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from frontend.app import _header


class TestTheViewImportsNothingAboveItself:
    def test_it_does_not_import_the_composition_root(self):
        src = pathlib.Path(_header.__file__).read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

        assert "backend.src.app" not in code

    def test_every_backend_import_is_a_controller(self):
        """The contract's actual rule, checked here directly so a new reach
        upward fails in this file's own test as well as in the gate."""
        tree = ast.parse(pathlib.Path(_header.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("backend.src"):
                assert node.module.startswith("backend.src.controllers"), node.module


class TestItIsHandedWhatItNeeds:
    def test_build_header_takes_the_injected_handles(self):
        params = inspect.signature(_header.build_header).parameters

        for name in ("get_engine", "admin_available", "admin_open_fn"):
            assert name in params, name

    def test_they_are_keyword_only(self):
        """Positional injection at a five-argument call site is how the wrong
        callable ends up in the admin button."""
        params = inspect.signature(_header.build_header).parameters

        for name in ("get_engine", "admin_available", "admin_open_fn"):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name


class TestTheCompositionRootSuppliesThem:
    def test_the_call_site_passes_all_three(self):
        src = pathlib.Path("frontend/app/__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "build_header"):
                passed = {kw.arg for kw in node.keywords}
                assert {"get_engine", "admin_available",
                        "admin_open_fn"} <= passed, passed
                return
        raise AssertionError("build_header(...) call not found in frontend/app/__init__.py")
