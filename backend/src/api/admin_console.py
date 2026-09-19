"""Find and mount the licence admin console, when this machine is the issuer.

The console is not part of this repository. It lives in `MooreSi/forex-admin`,
checked out at `~/forex-admin`, and it exists in two halves:

* a FastAPI router (`api.router`) mounted under `/api/admin`
* a compiled React bundle (`api.BUNDLE_DIR`) served at `/admin`

Why it is not vendored: it holds the Ed25519 private key that signs every
licence, and this repository ships to customers. The two must not be the same
tree. `backend/src/app.py` finds the same checkout for the NiceGUI console;
this module is the React app's equivalent, and deliberately uses the same
candidate order so the two cannot disagree about which checkout is live.

**This file must never make the app fail to start.** Every failure path here
returns None and logs. A trading app that will not boot because an optional
admin console is missing is a far worse outcome than a missing button, and
that trade-off has already been paid for once: on 2026-08-07 an iCloud-evicted
`licences.db` hung the NiceGUI app's startup inside this import for three
processes at once.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: Where to look, in order. `~/forex-admin` is the tracked git checkout and
#: must win. The other two are the untracked copies that predate it -- kept so
#: a machine that has not moved over still works, and slated for retirement
#: (forex-admin docs/simon-handover/002).
#:
#: `~/Documents` is iCloud-synced, which is why it is last: an evicted file
#: there is the 2026-08-07 startup hang.
CANDIDATES: tuple[Path, ...] = (
    Path.home() / "forex-admin",
    REPO_ROOT.parent / "KeyGen",
    Path.home() / "Documents" / "KeyGen",
)


def find_checkout() -> Optional[Path]:
    """The first candidate that looks like an admin console checkout.

    Identified by `api/routes.py` rather than by directory name, so a folder
    that merely has the right name does not qualify -- the legacy `KeyGen`
    copies have no `api/` at all and are correctly skipped for the React
    console while still serving the NiceGUI one.
    """
    for path in CANDIDATES:
        try:
            if (path / "api" / "routes.py").is_file():
                return path
        except OSError:                  # unreadable or stalled mount
            continue
    return None


def _import_api_package(checkout: Path) -> Any:
    """Import `<checkout>/api/__init__.py`, specifically that one.

    Not `importlib.import_module("api")`. `backend/src/app.py` inserts the
    NiceGUI console's directory at **sys.path[0]** when it finds one, so on a
    machine with both a `~/forex-admin` and a legacy `~/KeyGen` a plain
    `import api` can resolve to a different checkout than `find_checkout()`
    chose -- or to one already cached in `sys.modules` from earlier. The
    console would then be mounted from a tree this function never inspected.

    Found by this file's own tests polluting each other, which is the cheap
    version of finding it in production.

    The checkout still goes on `sys.path` (done by the caller) because the
    package imports its siblings -- `core.licence_rules` and, through
    `api/_siblings.py`, `database` and `licence_signing`.
    """
    import importlib.util

    init = checkout / "api" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "api", init, submodule_search_locations=[str(checkout / "api")],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"no importable api package at {init}")

    module = importlib.util.module_from_spec(spec)
    # Registered as "api" before exec: the package's own `from api.routes
    # import ...` has to resolve while it is still being executed.
    previous = sys.modules.get("api")
    sys.modules["api"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        if previous is not None:
            sys.modules["api"] = previous
        else:
            sys.modules.pop("api", None)
        raise
    return module


def load() -> Optional[Any]:
    """Import the console package, or return None with a reason in the log.

    Returns the imported `api` module, which carries `router`, `BUNDLE_DIR`
    and `console_available()` -- the contract documented in the forex-admin
    repo at `docs/system/rules/70-wiring-into-the-apps.md`.
    """
    # Through the controller, not straight into config: the API layer
    # reaches the backend through controllers, and that contract is enforced
    # at zero (tools.refactor_audit.import_contracts).
    from backend.src.controllers.remote_controller import is_licence_issuer_machine

    # The hardware pin leads, exactly as in app.py. A checkout on disk is a
    # filesystem fact, not an authorisation: ~/Documents is iCloud-synced, so
    # a console folder lands on every Mac on the owner's Apple ID.
    try:
        if not is_licence_issuer_machine():
            log.info("[Admin] Not the licence-issuer machine — the admin "
                     "console is not served here")
            return None
    except Exception as exc:             # noqa: BLE001 — never fail startup
        log.warning("[Admin] Could not determine issuer status (%s) — "
                    "console not served", exc)
        return None

    checkout = find_checkout()
    if checkout is None:
        log.debug("[Admin] No admin console checkout found in %s",
                  [str(p) for p in CANDIDATES])
        return None

    # APPENDED, not inserted at position 0. The console checkout has
    # top-level `api`, `core` and `database` modules, and putting it first
    # would let them shadow anything this app imports by those names. There is
    # no such name here today (checked 2026-09-19), and appending means there
    # never can be a problem if one is added later.
    added = str(checkout) not in sys.path
    if added:
        sys.path.append(str(checkout))

    def _unwind() -> None:
        """Take the checkout back off sys.path if we put it there.

        A checkout that failed to import must not be left on the path. It has
        top-level `api`, `core` and `database` modules, so leaving a broken
        one there means the next thing to import any of those names gets the
        broken copy -- a failure with no visible connection to the console.
        """
        if added and str(checkout) in sys.path:
            sys.path.remove(str(checkout))

    try:
        module = _import_api_package(checkout)
    except Exception as exc:             # noqa: BLE001 — reported, not raised
        log.warning("[Admin] Found a console at %s but could not import it: "
                    "%s — the Admin button will not appear", checkout, exc)
        _unwind()
        return None

    for name in ("router", "BUNDLE_DIR", "console_available"):
        if not hasattr(module, name):
            log.warning("[Admin] Console at %s is missing `%s` — it is a "
                        "different or older version than this app expects. "
                        "See that repo's docs/system/rules/70-wiring-into-the-apps.md",
                        checkout, name)
            _unwind()
            return None

    log.info("[Admin] Licence console loaded from %s", checkout)
    return module


def install(app: Any) -> bool:
    """Mount the console onto `app`. True when it was mounted.

    Mounting is two things: the API under `/api/admin` (inheriting this app's
    auth gate, because it sits under `/api`), and the compiled bundle at
    `/admin`.
    """
    from starlette.staticfiles import StaticFiles

    module = load()
    if module is None:
        return False

    bundle = Path(module.BUNDLE_DIR)
    if not (bundle / "index.html").is_file():
        # The API would work and the page would 404 -- worse than not
        # offering it, because the button would appear and go nowhere.
        log.warning("[Admin] Console API found but no compiled bundle at %s "
                    "— not mounting. Build it with: cd %s/web && npm install "
                    "&& npm run build", bundle, module.REPO_ROOT)
        return False

    app.include_router(module.router)
    app.mount("/admin", StaticFiles(directory=str(bundle), html=True),
              name="admin-console")
    log.info("[Admin] Licence console mounted at /admin")
    return True
