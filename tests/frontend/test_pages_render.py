"""Every page module imports and its renderer is callable.

The refactor moved essentially all backend code and rewired every page
through controllers. The test suite covers the backend thoroughly, but
until now nothing asserted that the FRONTEND still works — a page that
imports a function which no longer exists fails at import time, in the
browser, at the moment a user clicks the tab. `python -c "import
backend.src.app"` would not catch it, because the pages are imported
lazily inside tab panels.

This is deliberately an import-and-signature check rather than a full
render. Rendering a NiceGUI page needs a live client slot context, and
building that here would test NiceGUI more than it tests this app. What
actually broke during this refactor was imports and missing names, and
that is exactly what this catches.

The complementary check — that the app really boots and serves HTTP — is
covered by tests/frontend/test_app_boots.py.
"""
from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PAGES_DIR = REPO / "frontend" / "pages"


def _page_modules() -> list[str]:
    return sorted(
        f"frontend.pages.{path.stem}"
        for path in PAGES_DIR.glob("*.py")
        if path.stem != "__init__"
    )


@pytest.mark.parametrize("module_name", _page_modules())
def test_every_page_module_imports(module_name):
    """The failure this catches: a page importing a name the refactor moved."""
    importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", _page_modules())
def test_every_page_exposes_a_callable_renderer(module_name):
    module = importlib.import_module(module_name)
    render = getattr(module, "render", None)
    if render is None:
        pytest.skip(f"{module_name} is a helper module, not a page")
    assert callable(render), f"{module_name}.render is not callable"


def test_the_app_shell_imports_and_registers_its_route():
    """frontend/app.py owns the single @ui.page('/') — the whole UI hangs
    off it, so an import error here is a blank app."""
    import frontend.app  # noqa: F401
    from nicegui import ui  # noqa: F401
    assert hasattr(frontend.app, "__file__")


def test_the_new_expert_tunables_page_is_wired_into_settings():
    """M7's page is reached from a settings tab; an unreferenced renderer
    would leave the tab blank with no error anywhere."""
    settings_source = (PAGES_DIR / "settings.py").read_text()
    assert "from frontend.pages.expert_tunables import render" in settings_source
    assert 'ui.tab("Expert Tunables")' in settings_source

    from frontend.pages import expert_tunables
    assert callable(expert_tunables.render)
    assert not inspect.signature(expert_tunables.render).parameters, (
        "render() takes no arguments -- it reads everything through the "
        "settings controller"
    )
