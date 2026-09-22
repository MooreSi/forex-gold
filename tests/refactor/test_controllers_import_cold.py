"""Every controller must import on its own, as the first thing a process does.

Found 2026-09-18 while adding an endpoint to api/routers/node.py:

    ImportError: cannot import name '_ensure_sync_tables' from partially
    initialized module backend.src.services.cluster.sync_repo

The cycle was remote_node_controller -> services/cluster/node ->
services/cluster/sync_repo -> backend.src.db (package __init__) ->
db/database.py, whose module-level re-export block imported back up into
services/cluster/sync_repo while sync_repo was still on its own line 24.

Nothing caught it, and nothing could have:

* the app never hits it, because api/server.py imports other modules first
  and backend.src.db is fully initialised by the time a controller is
  reached;
* the suite never hits it, because tests/api/conftest.py imports
  backend.src.api.server at collection time.

So whether the process boots depended on which module happened to be
imported first -- one import-line reorder away from a boot failure, with no
gate watching. The same shape had already bitten analytics/read_repo once
(see the _LAZY block in db/database.py); this test is the gate that was
missing both times.

A subprocess per controller, because the only way to ask "does this import
cold" is to ask it in a process where nothing else has been imported yet.
The in-process form of this test cannot work: pytest has already imported
half the tree by the time it runs, and sys.modules cannot be unwound
faithfully.
"""
from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONTROLLERS = REPO / "backend" / "src" / "controllers"


def _controller_modules() -> list[str]:
    """Dotted names of every *_controller.py, re-derived each run so a
    controller added tomorrow is covered without editing this file."""
    return sorted(
        f"backend.src.controllers.{p.stem}"
        for p in CONTROLLERS.glob("*_controller.py")
    )


def _import_cold(module: str) -> tuple[str, str]:
    """(module, stderr) from a fresh interpreter that imports only `module`.

    `-c` rather than `-m`, so the import is the process's first statement
    and nothing about pytest's own import state can leak in. cwd is the repo
    root and sys.path[0] is "", which is how run.py starts the app.
    """
    proc = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return module, "" if proc.returncode == 0 else (proc.stderr or "").strip()


def test_every_controller_imports_as_the_first_import_of_a_process():
    modules = _controller_modules()
    assert modules, "no controllers found -- the glob is looking in the wrong place"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_import_cold, modules))

    broken = [(m, err) for m, err in results if err]
    assert not broken, (
        f"{len(broken)} of {len(modules)} controllers cannot be imported on "
        f"their own. Whichever module the process imports first then decides "
        f"whether it boots:\n\n"
        + "\n\n".join(f"{m}\n{err}" for m, err in broken)
    )


def test_the_runner_reports_an_import_that_actually_fails():
    """Negative control. A green result above is worth nothing unless this
    runner can go red -- and it reports failure through a subprocess exit
    code, which is exactly the kind of thing that silently always passes."""
    module, err = _import_cold("backend.src.controllers.no_such_controller")
    assert err, "a missing module was reported as a clean import"
    assert "ModuleNotFoundError" in err, err
    assert module == "backend.src.controllers.no_such_controller"


def test_the_runner_finds_the_controllers_it_claims_to_cover():
    """Second control: the glob is the other place this can go vacuously
    green. Names chosen because they are the two the cycle actually broke."""
    modules = _controller_modules()
    assert "backend.src.controllers.remote_node_controller" in modules
    assert "backend.src.controllers.chart_controller" in modules
    assert len(modules) > 10, modules
