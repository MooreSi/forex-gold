"""Handing trading control between this node and the paired one.

**Exactly one node may execute new trades.** Both sides are pointed at the same
MT5 account, so two active nodes is two engines opening positions against one
balance — the failure this whole cluster protocol exists to prevent.

The sequence lived in the NiceGUI header's `_toggle_mode` until 2026-09-18 and
is moved here unchanged. It is moved rather than rewritten deliberately: the
ordering IS the safety property, and each half is ordered so that a failure
leaves the account with **no** active trader rather than two.

  * **Taking over** (remote -> local): ask the peer to stand down and wait for
    its acknowledgement FIRST. Only once it has acknowledged is this node
    marked active and its engines started. A peer that does not answer leaves
    this node exactly as it was — view-only — because the alternative is this
    node trading while the VPS believes it still owns the account.

  * **Handing back** (local -> remote): stop this node's engines FIRST, then
    ask the peer to resume. If the peer does not acknowledge, the engines stay
    stopped and this node is still marked `remote_vps`. Nothing is trading,
    which is a state the operator can see and retry from.

`active_trader` gates only NEW entries. Positions this node already has open
keep being managed by its own monitor loop through a handover — trailing, SL
and TP all continue — so handing back does not abandon them. That is why the
result says how many are still running: the operator deserves to know that
"view-only" does not mean "flat".

Nothing here closes a position. The engines' `start`/`stop` are the same calls
the mode toggle has always made.
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.src.services.cluster import node as _node
from backend.src.services.cluster.sync import client as _client
from backend.src.services.cluster.sync.protocol import TRADER_REMOTE_VPS
from backend.src.services.engines import registry as _engines

log = logging.getLogger(__name__)

__all__ = ["take_over_locally", "take_over_without_peer", "hand_back_to_remote",
           "HandoverRefused"]

TRADER_LOCAL = "local"

# Which engines exist, and which of them a bulk start may touch, belong to
# services/engines/registry.py. This file had its own copy of that table until
# it was consolidated on 2026-09-18 -- two copies is how the empty `bounce`
# slot gets re-introduced in one and not the other.
_start_stopped_engines = _engines.start_stopped
_stop_running_engines = _engines.stop_running


class HandoverRefused(Exception):
    """The peer did not agree, so control did not move.

    Its message is for the operator: "the VPS did not acknowledge" is the
    difference between "try again" and "your account is being traded twice".
    """


def _paired_host() -> str:
    """The VPS this install is paired with, or "" when it is standalone.

    The SAME question `open_trade` asks before it applies its stand-down gate
    (`_host and get_active_trader() == TRADER_REMOTE_VPS`). Both sides must
    agree on what "there is a second node" means, or the flag can mean one
    thing to the order path and another to the screen.
    """
    try:
        from backend.src.services.cluster.sync.client import SyncClient
        host, _, _ = SyncClient.load_config()
        return host or ""
    except Exception as exc:
        # Unreadable is treated as PAIRED: assuming standalone here would skip
        # the stand-down handshake on a node that may well have a peer.
        log.warning("[handover] could not read the pairing config: %s", exc)
        return "unknown"


def _require_link() -> None:
    if _client.get_instance().conn_state != "connected":
        raise HandoverRefused(
            "Not connected to a remote node. Pair one in Settings > Node "
            "before switching which machine trades."
        )


async def take_over_locally(timeout: float = 15.0) -> dict:
    """Take control: the peer stands down, THEN this node starts trading.

    The order is the whole point. Marking this node active before the peer has
    acknowledged means two nodes believing they own the same account.
    """
    # A standalone install has no peer to stand down, and demanding one
    # traps it: `active_trader` can be `remote_vps` with nothing paired, the
    # order path ignores the flag entirely (so the node trades), and the only
    # control that clears it needs a link that does not exist. Found on the
    # owner's Mac, 2026-09-20.
    #
    # This does NOT weaken the mutual-exclusion guarantee. A CONFIGURED peer
    # still has to acknowledge, reachable or not, because a peer that cannot
    # be reached may still be trading the same account.
    if not _paired_host():
        _node.set_active_trader(TRADER_LOCAL)
        _start_stopped_engines()
        return {
            "active_trader": TRADER_LOCAL,
            "remote_open_positions": 0,
            "note": ("Now trading on this machine. There is no remote node "
                     "paired with this install, so nothing had to stand down."),
        }

    _require_link()
    try:
        ack = await _client.get_instance().request_stand_down(timeout=timeout)
    except Exception as exc:
        log.warning("[handover] peer did not acknowledge stand-down: %s", exc)
        raise HandoverRefused(
            f"The remote node did not acknowledge the stand-down ({exc}). "
            "Nothing changed — it is still the active trader."
        ) from exc

    _node.set_active_trader(TRADER_LOCAL)
    _start_stopped_engines()

    still_open = len((ack or {}).get("open_positions", []) or [])
    return {
        "active_trader": TRADER_LOCAL,
        "remote_open_positions": still_open,
        "note": (
            f"Now trading on this machine. The remote node stood down"
            + (f"; {still_open} of its position(s) keep running to their own "
               "SL/TP." if still_open else ".")
        ),
    }


async def take_over_without_peer() -> dict:
    """Take control when the paired peer cannot be reached, on the operator's
    word that it is not trading (owner's decision, 2026-09-25).

    Never a fallback inside `take_over_locally`, which still refuses an
    unreachable peer: this runs only when the operator presses it after
    confirming the VPS is off or its MT5 is closed. Refused while the link is
    up, because then the peer can be asked properly. The peer is told to stand
    down the moment it reconnects (SyncClient.stand_down_peer_if_local), which
    is what keeps "both trading" to the window the operator vouched for.
    """
    if _client.get_instance().conn_state == "connected":
        raise HandoverRefused(
            "The remote node is connected, so it can be asked to stand down "
            "properly. Use the normal switch instead."
        )
    _node.set_active_trader(TRADER_LOCAL)
    _start_stopped_engines()
    log.warning("[handover] took over WITHOUT the peer's acknowledgement, on the "
                "operator's confirmation that it is not trading")
    return {
        "active_trader": TRADER_LOCAL,
        "remote_open_positions": 0,
        "note": ("Now trading on this machine. The VPS could not be reached, so "
                 "it has not been told yet: it will be told to stand down as "
                 "soon as it reconnects."),
    }


async def hand_back_to_remote(
    timeout: float = 15.0, open_trades: Optional[list] = None,
) -> dict:
    """Hand control back: this node's engines stop, THEN the peer resumes.

    If the peer does not answer, the engines stay stopped and this node is
    still marked view-only. Nothing trading is a state the operator can see;
    two nodes trading is not.
    """
    _require_link()
    _stop_running_engines()

    try:
        await _client.get_instance().request_resume(timeout=timeout)
    except Exception as exc:
        # Engines are already stopped. Leave them stopped and record the
        # view-only state rather than guess whether the peer resumed.
        log.warning("[handover] peer did not acknowledge resume: %s", exc)
        _node.set_active_trader(TRADER_REMOTE_VPS)
        raise HandoverRefused(
            f"The remote node did not acknowledge ({exc}). This machine has "
            "stopped its engines and is now view-only, but the remote node may "
            "not have resumed — check it before leaving this."
        ) from exc

    _node.set_active_trader(TRADER_REMOTE_VPS)

    kept = len(open_trades or [])
    return {
        "active_trader": TRADER_REMOTE_VPS,
        "local_open_positions": kept,
        "note": (
            "Control handed back to the remote node. This is now view-only"
            + (f"; {kept} local position(s) keep running to their own SL/TP."
               if kept else ".")
        ),
    }
