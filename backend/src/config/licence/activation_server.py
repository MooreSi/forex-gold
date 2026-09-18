"""The activation and licence-error screens, served without a UI framework.

These run BEFORE the app starts, on the app's own port, and they are the only
way back into a stranded install — so they are deliberately plain: one HTML
document with no build step, no bundle to be stale, and no dependency on
`frontend/dist` existing. A licence screen that needs the dashboard to have
been compiled is a licence screen that cannot rescue a broken install.

Every decision lives in `activation.py`; this module renders and routes.

**It grants nothing.** The only path that stores a licence is
`activation.activate_manually`, which verifies the Ed25519 signature against
this machine first — the same check `enforce()` makes.
"""
from __future__ import annotations

import html
import logging
from contextlib import asynccontextmanager
from typing import Callable, Optional

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from backend.src.config.licence import activation

log = logging.getLogger(__name__)

# Defined HERE, and imported by the real app, which must 404 it: that is how
# the wait page knows the restart has happened. This direction and not the
# other, because `config/` sits at the bottom of the import stack and reaching
# up to `api/` for a string would be the lowest layer depending on the highest
# — which is exactly what the utils-and-config contract exists to stop.
PROBE_PATH = "/licence-activated/probe"

_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FOREX Trader — __TITLE__</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#030712; color:#e5e7eb;
         font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; }
  main { width:min(92vw, 32rem); background:#0b111e; border:1px solid #263044;
         border-radius:.5rem; padding:1.75rem; }
  h1 { margin:0 0 .25rem; font-size:1.25rem; color:#ffd700; }
  p.sub { margin:0 0 1.25rem; font-size:.8rem; color:#6b7280; }
  label { display:block; font-size:.75rem; color:#9ca3af; margin-top:.75rem; }
  input { width:100%; box-sizing:border-box; margin-top:.25rem; padding:.5rem;
          background:#030712; color:#e5e7eb; border:1px solid #263044;
          border-radius:.25rem; font-size:.875rem; }
  button { margin-top:1rem; width:100%; padding:.55rem; border-radius:.25rem;
           border:1px solid #263044; background:#1b2333; color:#e5e7eb;
           font-size:.8rem; cursor:pointer; }
  button.secondary { background:transparent; color:#9ca3af; margin-top:.5rem; }
  .machine { font-family: ui-monospace, Menlo, monospace; font-size:.8rem;
             background:#030712; border:1px solid #263044; border-radius:.25rem;
             padding:.5rem; margin-top:.25rem; word-break:break-all; }
  .status { margin-top:.9rem; font-size:.8rem; min-height:1.2rem; }
  .status[data-tone="bad"] { color:#ff4444; }
  .status[data-tone="warn"] { color:#ffb020; }
  .status[data-tone="good"] { color:#00cc88; }
  .notice { margin-bottom:1rem; padding:.6rem; border-radius:.25rem;
            border:1px solid rgba(255,176,32,.4); background:rgba(255,176,32,.08);
            color:#ffb020; font-size:.75rem; }
  details { margin-top:1rem; }
  summary { font-size:.75rem; color:#9ca3af; cursor:pointer; }
</style></head><body><main>__BODY__</main>__SCRIPT__</body></html>
"""

_SCRIPT = """<script>
const status = document.getElementById("status");
function say(message, tone) {
  status.textContent = message;
  status.dataset.tone = tone || "";
}
function details() {
  return {
    nickname: document.getElementById("nickname").value,
    email: document.getElementById("email").value,
  };
}
async function post(path, body) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return r.json();
}
document.getElementById("request").onclick = async () => {
  const answer = await post("/api/activation/register", details());
  say(answer.message, answer.ok ? "" : "warn");
  if (answer.ok) watchDelivery();
};
document.getElementById("activate").onclick = async () => {
  const body = details();
  body.code = document.getElementById("code").value;
  const answer = await post("/api/activation/manual", body);
  say(answer.message, answer.ok ? "good" : "bad");
};
let deliveryTimer = null;
function watchDelivery() {
  // One watcher, replaced rather than stacked: a second click used to add a
  // second timer alongside the first.
  if (deliveryTimer) clearInterval(deliveryTimer);
  deliveryTimer = setInterval(async () => {
    const r = await fetch("/api/activation/delivery");
    const d = await r.json();
    say(d.message, d.state === "delivered" ? "good"
        : d.state === "retrying" ? "warn" : "");
    if (d.state === "delivered") { clearInterval(deliveryTimer); deliveryTimer = null; }
  }, 2000);
}
setInterval(async () => {
  // The admin can push a licence down at any moment. When it lands, go to the
  // wait page BEFORE the process restarts, so the operator sees a page rather
  // than a dead socket.
  const r = await fetch("/api/activation/status");
  const s = await r.json();
  if (s.activated) window.location.href = "/licence-activated";
}, 1000);
</script>"""


class Details(BaseModel):
    nickname: str = ""
    email: str = ""


class ManualActivation(Details):
    code: str = ""


def _page(title: str, body: str, script: str = "") -> str:
    return (_PAGE.replace("__TITLE__", html.escape(title))
                 .replace("__BODY__", body)
                 .replace("__SCRIPT__", script))


def error_page(reason: str) -> str:
    return _page("Licence error", f"""
      <h1>Licence error</h1>
      <p class="sub">This install cannot start.</p>
      <div class="notice">{html.escape(reason or "No valid licence was found.")}</div>
      <p class="sub">Contact your administrator for assistance.</p>
    """)


def activation_page(machine_id: str, notice: str = "") -> str:
    banner = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
    return _page("Activate", f"""
      <h1>FOREX Trader</h1>
      <p class="sub">Licence activation required.</p>
      {banner}
      <label>Your machine ID</label>
      <div class="machine">{html.escape(machine_id)}</div>
      <label for="nickname">Name or nickname</label>
      <input id="nickname" autocomplete="nickname">
      <label for="email">Email</label>
      <input id="email" type="email" autocomplete="email">
      <button id="request">Request registration</button>
      <div id="status" class="status"></div>
      <details>
        <summary>Manual activation</summary>
        <label for="code">Licence key</label>
        <input id="code" placeholder="Paste the key provided by your administrator">
        <button id="activate" class="secondary">Activate manually</button>
      </details>
    """, _SCRIPT)


def waiting_page() -> str:
    return _page("Starting", f"""
      <h1>Licence activated</h1>
      <p class="sub">The app is restarting. This page moves on by itself.</p>
      <p class="sub"><a href="/" style="color:#64b4ff">Open FOREX Trader</a> if it does not.</p>
    """, f"""<script>
      setInterval(async () => {{
        try {{
          const r = await fetch("{PROBE_PATH}");
          // While THIS process still answers the probe, the restart has not
          // happened yet. A 404 means the real app is on the port now.
          if (r.status === 404) window.location.replace("/");
        }} catch (e) {{ window.location.replace("/"); }}
      }}, 1500);
    </script>""")


def build_app(
    machine_id: str,
    *,
    notice: str = "",
    error: Optional[str] = None,
    on_startup: Optional[list[Callable[[], None]]] = None,
) -> FastAPI:
    """The activation app. `error` makes it the read-only error screen instead.

    `on_startup` callables are run inside the loop and each failure is
    swallowed: this screen is the only way back into a stranded install, and an
    exception here replaces it with a traceback.
    """

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        for fn in on_startup or []:
            try:
                fn()
            except Exception as exc:
                log.warning("Activation agent %s failed to start: %s",
                            getattr(fn, "__name__", fn), exc)
        yield

    app = FastAPI(title="FOREX Trader — activation", docs_url=None,
                  redoc_url=None, lifespan=_lifespan)

    if error is not None:
        @app.get("/{full_path:path}", response_class=HTMLResponse)
        async def _error(full_path: str):          # noqa: ANN202
            return error_page(error)
        return app

    @app.get("/", response_class=HTMLResponse)
    async def _index():                            # noqa: ANN202
        return activation_page(machine_id, notice)

    @app.get("/licence-activated", response_class=HTMLResponse)
    async def _activated():                        # noqa: ANN202
        return waiting_page()

    @app.get(PROBE_PATH)
    async def _probe():                            # noqa: ANN202
        return {"stage": "activation"}

    @app.post("/api/activation/register")
    async def _register(body: Details):            # noqa: ANN202
        complaint = activation.validate_details(body.nickname, body.email)
        if complaint:
            return {"ok": False, "message": complaint}
        from backend.src.services.cluster.remote import client as _rc

        # request_registration() queues a connection and starts ONE loop.
        # Calling start() as well would spawn a second, double the server's
        # failure count and trip the rate limiter before registration gets
        # through.
        _rc.request_registration(body.email.strip(), body.nickname.strip())
        return {"ok": True, "message": "Contacting administrator server..."}

    @app.get("/api/activation/delivery")
    async def _delivery():                         # noqa: ANN202
        from backend.src.services.cluster.remote import client as _rc

        return activation.describe_delivery(_rc.get_status())

    @app.post("/api/activation/manual")
    async def _manual(body: ManualActivation):     # noqa: ANN202
        return activation.activate_manually(
            machine_id, body.nickname, body.email, body.code)

    @app.get("/api/activation/status")
    async def _status():                           # noqa: ANN202
        from backend.src.services.cluster.remote import client as _rc

        return {"activated": bool(_rc.licence_activated.is_set())}

    @app.exception_handler(404)
    async def _not_found(request, exc):            # noqa: ANN001, ANN202
        # Anything else goes back to the form rather than a bare 404: this is
        # the only page on this process and a wrong URL should not look like a
        # broken install.
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=404, content={"error": "no such endpoint"})
        return HTMLResponse(activation_page(machine_id, notice))

    return app
