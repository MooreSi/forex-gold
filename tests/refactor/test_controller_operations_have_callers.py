"""A controller operation exists to be called by something that is not a test.

The layering rule is `frontend/ → controllers/ → services/`, and a controller
is *"a flat `<name>_controller.py` that names an operation and forwards it to
one service"*. So every name a controller exports is a route the UI uses. One
that nothing in `backend/` or `frontend/` mentions is a route to nowhere.

The layer is in good shape: **252 of 255 exported operations are referenced.**
That is what makes this worth gating rather than baselining — three exceptions,
not three hundred.

**A test caller does not count, and `engines_running` is why.** It is exported,
it is defined, and `tests/controllers/test_engines_controller_lifecycle.py`
calls it — so the coverage report shows it green and the suite proves it works.
Nothing in the app has ever called it. A test that keeps dead code looking alive
is the exact shape this repo's rules were written after: *"an audit found ~3,000
lines of extracted code nothing called"*.

Shrink-only. The known-dead set may lose entries and must never gain one.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from tests.refactor._source_scan import REPO, references_to

_CONTROLLERS = sorted(
    p.as_posix() for p in (REPO / "backend/src/controllers").glob("*_controller.py")
)

# Known dead. Each is a controller operation no page calls.
KNOWN_DEAD = {
    # Exported and tested, called by nothing. See the docstring.
    ("engines_controller", "engines_running"),
    # Named in its own module docstring as the one that returns a service
    # object -- and nothing asks it for one.
    ("sync_controller", "make_stats_facades"),
    ("sync_controller", "server_is_running"),
}


def _rel(path: str) -> str:
    return Path(path).relative_to(REPO).as_posix()


def _dead() -> set[tuple[str, str]]:
    out = set()
    for path in _CONTROLLERS:
        rel = _rel(path)
        mod = importlib.import_module(rel[:-3].replace("/", "."))
        for name in getattr(mod, "__all__", []):
            if name.startswith("_"):
                continue
            if not references_to(name, exclude=(rel,)):
                out.add((Path(rel).stem, name))
    return out


class TestEveryControllerOperationIsCalled:
    def test_no_new_routes_to_nowhere(self):
        unexpected = _dead() - KNOWN_DEAD

        assert not unexpected, (
            f"controller operations nothing calls: {sorted(unexpected)} — a "
            "controller names an operation for a page to use. Wire it up or "
            "delete it; do not add it to KNOWN_DEAD."
        )

    def test_the_known_dead_set_has_no_slack(self):
        assert _dead() == KNOWN_DEAD

    def test_the_layer_is_overwhelmingly_alive(self):
        """The number that makes this a gate rather than a wish. If exports
        ever drift far above callers, this file is measuring the wrong thing
        and should be re-argued rather than baselined."""
        exported = 0
        for path in _CONTROLLERS:
            mod = importlib.import_module(_rel(path)[:-3].replace("/", "."))
            exported += sum(1 for n in getattr(mod, "__all__", [])
                            if not n.startswith("_"))

        assert exported > 200
        assert len(_dead()) <= 5


class TestTheScannerCanSee:
    def test_a_live_operation_is_seen_as_live(self):
        assert references_to("get_risk_settings", exclude=())

    def test_a_name_nothing_mentions_is_seen_as_dead(self):
        assert not references_to("controller_operation_that_never_existed", exclude=())

    def test_tests_are_not_searched(self):
        """`engines_running` HAS a caller, in tests/. The scan looks only at
        backend/ and frontend/ on purpose, and this is the assertion that says
        so -- widen it to tests/ and this gate stops finding anything."""
        assert not references_to(
            "engines_running",
            exclude=("backend/src/controllers/engines_controller.py",))


@pytest.mark.parametrize("path", _CONTROLLERS)
def test_each_controller_declares_its_surface(path):
    """A controller with no `__all__` is invisible to this gate."""
    mod = importlib.import_module(_rel(path)[:-3].replace("/", "."))
    assert getattr(mod, "__all__", None), _rel(path)
