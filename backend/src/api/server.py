"""The composition root: builds the ASGI app that `run.py` serves.

This is the one module in `backend/src/api/` permitted to import
`backend.src.app` — it holds the live `TradingRuntime` and hands it to
`deps.set_engine_provider()` so every router receives it injected. That is the
same arrangement `frontend/app/__init__.py` had under NiceGUI, and it carries
the same single named exemption in the import contracts, for the same reason:
something has to wire the app up, and naming the file is honest where a
baseline of 1 cannot tell a sanctioned site from the next one somebody adds.

**Mount order is load-bearing.** Routers are registered first and the SPA
fallback last. The fallback answers any unmatched path with `index.html` so a
client-side route survives a browser refresh; registered before the routers it
would swallow `/api/*` and every endpoint would return HTML with a 200, which
reads as "the API returns nonsense" rather than as a routing bug.
`tests/api/test_static_bundle_is_served.py` pins it.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from backend.src.api import auth as gate
from backend.src.api import deps, errors
from backend.src.api.routers import ai as ai_router
from backend.src.api.routers import ai_settings as ai_settings_router
from backend.src.api.routers import auth as auth_router
from backend.src.api.routers import backtest as backtest_router
from backend.src.api.routers import chart as chart_router
from backend.src.api.routers import decision_log as decision_log_router
from backend.src.api.routers import engines as engines_router
from backend.src.api.routers import environment as environment_router
from backend.src.api.routers import history as history_router
from backend.src.api.routers import news as news_router
from backend.src.api.routers import notifications as notifications_router
from backend.src.api.routers import node as node_router
from backend.src.api.routers import parsing as parsing_router
from backend.src.api.routers import remote as remote_router
from backend.src.api.routers import orb as orb_router
from backend.src.api.routers import orders as orders_router
from backend.src.api.routers import schedule as schedule_router
from backend.src.api.routers import settings as settings_router
from backend.src.api.routers import system as system_router
from backend.src.api.routers import templates as templates_router
from backend.src.api.routers import trading as trading_router

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
BUNDLE_DIR = REPO_ROOT / "frontend" / "dist"

# The path the pre-boot activation screen answers and this app must NOT. Taken
# from that module rather than repeated, so the two cannot drift into a pair of
# paths that never agree — and in that direction, because `config/` is below
# `api/` in the import stack. See `_spa_fallback`.
from backend.src.config.licence.activation_server import (  # noqa: E402
    PROBE_PATH as ACTIVATION_PROBE,
)
STATIC_DIR = REPO_ROOT / "frontend" / "static"

ROUTERS = (
    system_router.router,
    auth_router.router,
    ai_router.router,
    ai_settings_router.router,
    backtest_router.router,
    chart_router.router,
    decision_log_router.router,
    engines_router.router,
    environment_router.router,
    history_router.router,
    news_router.router,
    notifications_router.router,
    node_router.router,
    parsing_router.router,
    remote_router.router,
    schedule_router.router,
    settings_router.router,
    templates_router.router,
    trading_router.router,
    orb_router.router,
    orders_router.router,
)


def _default_reader_provider() -> Callable[[], Any]:
    """The Telegram reader handle, imported lazily for the same reason as the
    engine: building an app for a test must not start the application graph."""
    from backend.src.app import get_tg_reader
    return get_tg_reader


def _default_engine_provider() -> Callable[[], Any]:
    """Imported lazily so building an app for a test does not start the engine.

    `backend.src.app` is a heavy import with module-level engine state; a test
    that passes its own provider must never pay for it.
    """
    from backend.src.app import get_engine
    return get_engine


@asynccontextmanager
async def _lifecycle(app: FastAPI):                       # noqa: ANN201
    """Start the engine, bridge, Telegram reader and sync on boot; stop them on
    shutdown.

    Both halves already live in `backend/src/app.py` — they were extracted
    there so the NiceGUI app and the headless entry point could share one
    implementation. This is a third caller of the same two functions, not a
    third copy: a sub-engine wired in there still applies here with nothing to
    remember to port.
    """
    from backend.src.app import shutdown as _shutdown
    from backend.src.app import startup as _startup
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


def build_app(
    *,
    engine_provider: Optional[Callable[[], Any]] = None,
    reader_provider: Optional[Callable[[], Any]] = None,
    bundle_dir: Optional[Path] = None,
    session_secret: str = "dev-only-not-a-secret",
    install_auth_gate: bool = True,
    with_lifecycle: bool = False,
) -> FastAPI:
    """Build the ASGI app.

    `with_lifecycle` is off by default and `run.py` is the only caller that
    turns it on. A test that built an app with the lifecycle attached would
    start the real trading engine, connect the bridge and log into Telegram —
    which is not a fixture, it is production.
    """
    app = FastAPI(
        title="FOREX Trader",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifecycle if with_lifecycle else None,
    )

    deps.set_engine_provider(engine_provider or _default_engine_provider())
    deps.set_reader_provider(reader_provider or _default_reader_provider())
    errors.install(app)

    # Middleware runs outermost-first in reverse registration order, so the
    # session must be added AFTER the gate for the gate to see a decoded
    # session on the request.
    if install_auth_gate:
        app.add_middleware(gate.AuthGate)
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        same_site="lax",
        # No `https_only`: this serves http://localhost by design, and setting
        # it would drop the cookie and lock the operator out of their own
        # dashboard rather than protect anything.
        https_only=False,
    )

    for router in ROUTERS:
        app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict:                      # noqa: ANN202
        return {"ok": True}

    _mount_bundle(app, bundle_dir if bundle_dir is not None else BUNDLE_DIR)
    return app


def _mount_bundle(app: FastAPI, bundle: Path) -> None:
    """Serve the compiled React bundle, and fall back to its index.

    The fallback is **middleware, not a catch-all route**, and that is the
    whole point of this function. A `@app.get("/{full_path:path}")` route
    full-matches every path, which defeats Starlette's partial-match handling:
    a GET to a POST-only endpoint stops being a 405 and becomes whatever the
    catch-all returns. On the money router that matters — "GET /orders/market
    returns 404" reads as "that endpoint does not exist" and sends the next
    person looking for a bug that is not there. Middleware runs after routing,
    so a real 405 stays a 405 and only a genuine 404 is reconsidered.

    A source checkout with no `dist/` must still start and say why: an operator
    whose UI is missing needs "run npm run build", not a stack trace from a
    StaticFiles mount that could not find its directory.
    """
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    index = bundle / "index.html"
    have_bundle = index.is_file()
    if not have_bundle:
        log.warning(
            "No compiled dashboard at %s — API is up, UI is not. "
            "Build it with: cd frontend && npm install && npm run build", bundle,
        )
    elif (bundle / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(bundle / "assets")),
                  name="assets")

    @app.middleware("http")
    async def _spa_fallback(request, call_next):        # noqa: ANN001, ANN202
        response = await call_next(request)
        if response.status_code != 404:
            return response
        path = request.url.path
        # An unknown /api path is a real 404 and says so. Answering it with the
        # index would hide a routing mistake behind a 200 and an empty panel.
        #
        # ACTIVATION_PROBE is in the same list for a different and sharper
        # reason: the pre-boot activation screen registers that path, and its
        # "please wait, restarting" page polls it and redirects to the app the
        # moment it 404s. Serving the SPA there would answer 200 for ever, and
        # the operator would sit on a restarting page that never finishes. The
        # real app 404ing this path IS the signal.
        if (path.startswith("/api/") or path == "/healthz"
                or path == ACTIVATION_PROBE):
            return JSONResponse(
                status_code=404,
                content={"error": {"kind": "not_found",
                                   "message": f"No such endpoint: {path}",
                                   "ref": None}},
            )
        if not have_bundle:
            return JSONResponse(
                status_code=503,
                content={"error": {
                    "kind": "no_bundle",
                    "message": ("The dashboard has not been built. Run "
                                "`npm install && npm run build` in frontend/."),
                    "ref": None,
                }},
            )
        candidate = bundle / path.lstrip("/")
        if path != "/" and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)

    if have_bundle:
        @app.get("/", include_in_schema=False)
        async def _index():                              # noqa: ANN202
            return FileResponse(index)
