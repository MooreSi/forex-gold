"""Settings > Remote Node — this machine's role in the Local/Remote pair.

Exactly one role per machine: the VPS accepts the connection, the Mac initiates
it. Both use the same shared token, generated on the VPS. The protocol is in
`services/cluster/sync/`; this is the surface that configures it.

Ported on 2026-09-18. The NiceGUI page (`frontend/pages/remote_node.py`, 270
lines) was deleted in the big-bang replace and had no React replacement, which
left five things unreachable: starting and stopping the sync server, connecting
out to a VPS, headless mode, centralized signal generation, and the one-off
model-snapshot copy.

**Nothing here places an order**, and two things here change what the engines
do next:

  * **Centralized signal generation.** With it on and the VPS the active
    trader, the VPS stops analysing and parsing entirely and only executes what
    this machine forwards. If this machine goes offline the VPS does NOT fall
    back to generating its own signals — it alerts and waits. The response
    repeats that, because a switch whose consequence is "and then nothing
    trades" must not be a bare toggle.
  * **Headless mode** restarts the app.

The tokens are write-only through this layer, like every other credential: the
state says whether one is stored, never what it is.
"""
from __future__ import annotations

import logging
import socket
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.src.api.deps import engine as engine_dep
from backend.src.api.errors import Refusal
from backend.src.controllers import engines_controller as engines_ctl
from backend.src.controllers import remote_node_controller as node_ctl
from backend.src.controllers import sync_controller as sync_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/remote", tags=["remote"])

SERVER_ENABLED = "sync_server_enabled"
SERVER_PORT = "sync_server_port"
HEADLESS = "headless_mode_enabled"


class ServerWrite(BaseModel):
    enabled: bool
    port: int = 0


class PortWrite(BaseModel):
    port: int = 0


class ClientWrite(BaseModel):
    host: str = ""
    port: int = 0
    token: str = ""


class Toggle(BaseModel):
    enabled: bool


class SnapshotWrite(BaseModel):
    direction: str


def _flag(key: str) -> bool:
    return bool(int(node_ctl.get_app_config(key) or "0"))


def _listen_host() -> str:
    """This machine's address on its own network, or every interface.

    `0.0.0.0` on failure is the same fallback the page used: a VPS that cannot
    resolve its own hostname should still accept the connection rather than
    refuse to start.
    """
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "0.0.0.0"


@router.get("/state")
async def state() -> dict:
    """Both roles, the live link and the two behaviour switches, in one read."""
    host, port, token = sync_ctl.load_config()
    link = sync_ctl.link_state()
    listen_port = int(node_ctl.get_app_config(SERVER_PORT) or sync_ctl.DEFAULT_SYNC_PORT)
    return {
        "server": {
            "enabled": _flag(SERVER_ENABLED),
            "port": listen_port,
            "reachability": node_ctl.server_reachability(listen_port),
            "running": sync_ctl.server_is_running(),
            # Empty until the first start, which is when it is generated.
            "fingerprint": sync_ctl.cert_fingerprint() or "",
            "token_set": bool(node_ctl.get_sync_token()),
        },
        "client": {
            "host": host,
            "port": int(port or sync_ctl.DEFAULT_SYNC_PORT),
            "token_set": bool(token),
            "conn_state": link["conn_state"],
            "last_error": link["last_error"],
            # Only meaningful while connected; the browser shows it as the
            # peer's own numbers rather than this machine's.
            "remote_status": link["remote_status"] if link["conn_state"] == "connected" else {},
        },
        "headless": _flag(HEADLESS),
        "centralized_signal_gen": bool(
            node_ctl.get_risk_settings().get("centralized_signal_gen_enabled")),
    }


@router.put("/server")
async def set_server(body: ServerWrite, eng: Any = Depends(engine_dep)) -> dict:
    """Accept connections from a paired machine, or stop accepting them.

    The setting is stored either way, so a failed start does not leave the
    dashboard and the database disagreeing about what was asked for. Starting
    without a token is refused rather than attempted: the listener would come
    up and reject every connection, which looks identical to a network problem.
    """
    port = int(body.port or node_ctl.get_app_config(SERVER_PORT)
               or sync_ctl.DEFAULT_SYNC_PORT)
    node_ctl.set_app_config(SERVER_ENABLED, "1" if body.enabled else "0")
    node_ctl.set_app_config(SERVER_PORT, str(port))

    if not body.enabled:
        await sync_ctl.server_stop()
        return await state()

    token = node_ctl.get_sync_token()
    if not token:
        node_ctl.set_app_config(SERVER_ENABLED, "0")
        raise Refusal("Generate a pairing token first — Settings > Node.")

    breakout, bounce, reversal = engines_ctl.sub_engines()
    try:
        await sync_ctl.server_start(
            _listen_host(), port, token, main_engine=eng,
            breakout_engine=breakout, bounce_engine=bounce, re_engine=reversal,
        )
    except Exception as exc:
        # Recorded as off: the listener is not up, and a stored "on" would have
        # the next restart claim it is.
        node_ctl.set_app_config(SERVER_ENABLED, "0")
        log.warning("[remote] sync server failed to start: %s", exc)
        raise Refusal(f"The sync server did not start: {exc}") from exc
    return await state()


_FIREWALL_NOTE = {
    "open": "Port {port} is open in the Windows firewall.",
    "declined": ("Port {port} is not open: the Windows permission prompt was "
                 "declined. Press the button again, or run the command below "
                 "as administrator."),
    "not-applicable": "Make sure port {port} is open in this machine's firewall.",
}


@router.post("/make-vps")
async def make_vps(body: PortWrite, eng: Any = Depends(engine_dep)) -> dict:
    """One press: a pairing token if there is none, the sync port open in the
    Windows firewall, and the listener started. The installer does not open the
    port (owner, 2026-09-25), because most installs are someone's main PC.

    An existing token is kept: a new one would silently unpair the machine
    already using the old. A new one is returned here once and never again.
    """
    port = int(body.port or node_ctl.get_app_config(SERVER_PORT)
               or sync_ctl.DEFAULT_SYNC_PORT)
    new_token = None if node_ctl.get_sync_token() else node_ctl.generate_sync_token()
    firewall = await node_ctl.open_firewall(port)
    out = await set_server(ServerWrite(enabled=True, port=port), eng)
    out["note"] = ("This machine is now the VPS. "
                   + _FIREWALL_NOTE.get(firewall, "").format(port=port))
    if new_token:
        out["token"] = new_token
    return out


@router.post("/stop-vps")
async def stop_vps() -> dict:
    """Stop listening and close the port again. The token is kept, so coming
    back later does not unpair the other machine."""
    port = int(node_ctl.get_app_config(SERVER_PORT) or sync_ctl.DEFAULT_SYNC_PORT)
    node_ctl.set_app_config(SERVER_ENABLED, "0")
    await sync_ctl.server_stop()
    firewall = await node_ctl.close_firewall(port)
    out = await state()
    out["note"] = ("This machine is no longer the VPS."
                   + (f" Port {port} could not be closed: the Windows permission "
                      "prompt was declined." if firewall == "declined" else ""))
    return out


@router.put("/client")
async def connect(body: ClientWrite) -> dict:
    """Save the VPS address and connect to it.

    Saved and connected are one action on purpose: a stored address that was
    never dialled is the shape of bug where the operator believes they are
    paired and the link has never been up.
    """
    host = body.host.strip()
    token = body.token.strip()
    if not host:
        raise Refusal("The VPS address is needed.")
    if not token:
        # Not "keep the stored one": a wrong token looks exactly like a
        # network failure, and re-using a stale one hides which it is.
        raise Refusal("The shared token from the VPS is needed.")

    port = int(body.port or sync_ctl.DEFAULT_SYNC_PORT)
    sync_ctl.configure(host, port, token)
    sync_ctl.start(host, port, token)
    return await state()


@router.post("/client/disconnect")
async def disconnect() -> dict:
    sync_ctl.stop()
    return await state()


@router.put("/headless")
async def set_headless(body: Toggle, eng: Any = Depends(engine_dep)) -> dict:
    """Run without the web UI at all. Takes effect on restart, so it restarts.

    The restart is the honest behaviour: the setting reads as done the moment
    it is flicked, and a mode that only arrives some hours later when something
    else restarts the app is a mode nobody can reason about.
    """
    node_ctl.set_app_config(HEADLESS, "1" if body.enabled else "0")
    note = await node_ctl.restart_app(eng)
    return {"headless": body.enabled, "note": note}


@router.put("/centralized-signals")
async def set_centralized(body: Toggle) -> dict:
    """Generate signals on this node only and forward them to the trader.

    The note is not decoration. With this on, a VPS that loses contact with
    this machine stops receiving signals and does not start generating its
    own — it alerts and waits. An operator who turns this on and then closes
    the laptop has stopped trading without meaning to.
    """
    node_ctl.update_risk_settings(
        {"centralized_signal_gen_enabled": 1 if body.enabled else 0})
    return {
        "centralized_signal_gen": body.enabled,
        "note": (
            "This machine is now the only source of new signals. If it goes "
            "offline the remote node will alert and wait — it does not fall "
            "back to generating its own."
            if body.enabled else
            "Each node generates its own signals again."
        ),
    }


@router.post("/model-snapshot")
async def model_snapshot(body: SnapshotWrite) -> dict:
    """One-off copy of the trained model files, either direction.

    Manual and never automatic: it overwrites the destination's models, which
    is right for seeding a fresh node and wrong for anything on a timer.
    """
    if body.direction not in ("download", "upload"):
        raise Refusal("Direction must be 'download' or 'upload'.", status_code=400)
    if not sync_ctl.is_connected():
        raise Refusal("Not connected to a remote node.")
    try:
        await sync_ctl.request_model_snapshot(body.direction)
    except Exception as exc:
        raise Refusal(f"The transfer failed: {exc}") from exc
    return {"direction": body.direction, "note": f"Model snapshot {body.direction} complete."}
