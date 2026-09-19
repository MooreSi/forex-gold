"""Acting on the node that is actually trading, not the one being looked at.

When the VPS is the active trader, this machine's own sub-engines are stood
down. Pressing Stop on a panel here therefore stops an engine that was not
running, on a node that is not trading, while the VPS's copy keeps generating
signals — and the screen says "stopped". The sync server's own
`_handle_engine_control` names that failure: *"those buttons would otherwise
act on the Mac's own stood-down engine instance, which does nothing useful
while looking like it worked."*

The NiceGUI panels handled it and the React port did not, so between 2026-09-18
and this module every Signal Generator control was local-only.

Three things live here:

  * **Where a control should land.** `is_remote_active()` is the answer, and it
    is deliberately NOT "am I in Remote mode": under centralized signal
    generation the engines have moved HERE, so the local ones are the current
    ones even though the VPS is trading. That distinction is
    `remote_stats_facade`'s and this module defers to it rather than
    re-deriving it.

  * **What the panel should show.** In Remote mode the settings the engines
    obey are the peer's, so the peer's confirmed snapshot is overlaid on the
    local row. A panel that showed this node's settings while the VPS traded
    would be describing a machine nobody is watching.

  * **Reading "current" from the right place before toggling.** A toggle
    computes its new value from the current one. Taking that from the local DB
    row while writing to the peer makes the toggle one-directional: it
    recomputes the same "current" on every click and re-sends the same target
    state for ever. That was confirmed live — Bounce stuck OFF, Breakout stuck
    ON — and `note_remote_setting` exists to close it: the peer's ack is
    written into the snapshot so the next click sees it.

**Nothing here places, closes or sizes a trade.** Starting an engine lets it
generate signals again, which is the same authority the panel has always had.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.src.services.cluster.sync import client as _client
from backend.src.services.cluster.sync import remote_stats_facade as _facade
from backend.src.services.engines import registry as _engines
from backend.src.services.risk import settings as _risk

log = logging.getLogger(__name__)

__all__ = [
    "is_remote_active", "is_centralized_remote_mode", "where",
    "effective_settings", "set_engine_running", "set_ai_eval",
    "place_market_order", "AI_EVAL_KEYS", "RemoteControlFailed",
]

# The only two settings the sync protocol can carry. `set_ai_eval` is the one
# risk-settings flag `_handle_engine_control` understands; everything else has
# no remote route at all, which is why `effective_settings` shows the peer's
# values but no other switch offers to write them.
AI_EVAL_KEYS = {"breakout": "bo_claude_eval_enabled",
                "bounce": "sg_claude_eval_enabled"}


class RemoteControlFailed(Exception):
    """The peer did not accept it, so nothing changed on either node.

    Its message is for the operator. "The VPS rejected this" and "the VPS is
    unreachable" lead to different actions, and both beat a screen that quietly
    shows the new state.
    """


def is_remote_active() -> bool:
    """True when a control here must be sent to the peer instead of applied."""
    return _facade._is_remote_active()


def is_centralized_remote_mode() -> bool:
    """True on the node that generates locally while the VPS trades."""
    return _facade._is_centralized_remote_mode()


def where() -> str:
    """Which node a control will land on, for the panel to say out loud.

    Three states, not two. "centralized" is the case an operator would
    otherwise misread: the VPS is trading, so the header says REMOTE, and yet
    these engines are the live ones because generation moved here.
    """
    if is_centralized_remote_mode():
        return "centralized"
    return "remote" if is_remote_active() else "local"


def effective_settings(local: dict) -> dict:
    """The settings the engines are actually obeying.

    The peer's confirmed snapshot wins where it has an opinion, and only there:
    it carries the handful of keys the broadcast sends, not the whole row, so a
    wholesale replacement would blank every setting the peer never mentions.
    """
    if not is_remote_active():
        return dict(local or {})
    try:
        remote = dict(_client.get_instance().remote_settings or {})
    except Exception as exc:                      # pragma: no cover - defensive
        log.debug("[remote_control] no remote settings snapshot: %s", exc)
        return dict(local or {})
    return {**(local or {}), **remote}


def _current(key: str, local: dict, default: int = 1) -> bool:
    """The value a toggle should invert.

    From the peer's snapshot when the peer is the one that will receive the
    write. Reading the local row here is what made the toggle one-directional.
    """
    return bool(effective_settings(local).get(key, default))


async def set_engine_running(name: str, running: bool) -> dict:
    """Start or stop one engine, wherever it is actually running."""
    if not is_remote_active():
        engine = _engines.instance(name)
        if engine is None:
            return {"engine": name, "running": False, "built": False,
                    "where": "local"}
        if running:
            engine.start()
        else:
            engine.stop()
        return {"engine": name, "running": bool(getattr(engine, "is_running", False)),
                "built": True, "where": "local"}

    ack = await _send(name, "start" if running else "stop")
    return {"engine": name, "running": bool(ack.get("is_running", running)),
            "built": True, "where": "remote"}


async def set_ai_eval(engine: str, enabled: Optional[bool] = None) -> dict:
    """Turn the AI review of this engine's signals on or off.

    `enabled=None` means "invert whatever is current", which is what a toggle
    wants and the only form that needs the snapshot read to be right.
    """
    key = AI_EVAL_KEYS.get(engine)
    if key is None:
        raise RemoteControlFailed(
            f"AI evaluation is not a setting the {engine} engine has.")

    local = _risk.get() or {}
    target = (not _current(key, local)) if enabled is None else bool(enabled)

    if not is_remote_active():
        _risk.update({key: 1 if target else 0})
        return {"engine": engine, "key": key, "enabled": target, "where": "local"}

    await _send(engine, "set_ai_eval", enabled=target)
    # The peer acked, so record it in the snapshot the next click will read.
    # Without this the toggle recomputes the same "current" for ever.
    _note(key, 1 if target else 0)
    return {"engine": engine, "key": key, "enabled": target, "where": "remote"}


async def place_market_order(engine: Any, **order: Any) -> dict:
    """Place a market order on whichever node is actually trading.

    In Remote mode this node is stood down and `open_trade` refuses with
    "Trading stood down -- the VPS is the active trader". That is safe, and it
    is also a lost capability: the NiceGUI button forwarded the order over the
    sync channel so it executed on the machine holding the account. This is
    that forwarding.

    **No fallback.** A peer that cannot be reached is a refusal, not a reason
    to place the order here: here is either stood down, or a node the operator
    believes is idle.

    A local ValueError is the engine saying no with a reason the operator needs
    to read -- "DPM is disabled and no stop loss was given" -- so it is left to
    propagate rather than wrapped.
    """
    if not is_remote_active():
        result = await engine.open_manual_market_order(**order)
        return {**(result or {}), "where": "local"}

    try:
        ack = await _client.get_instance().send_market_order(**order)
    except Exception as exc:
        log.warning("[remote_control] market order did not reach the peer: %s", exc)
        raise RemoteControlFailed(
            f"The remote node could not be reached ({exc}). Nothing was placed."
        ) from exc
    if (ack or {}).get("error"):
        raise RemoteControlFailed(f"The remote node refused: {ack['error']}")
    return {**((ack or {}).get("result") or {}), "where": "remote"}


async def _send(engine: str, action: str, **kwargs: Any) -> dict:
    """One engine-control message, with the peer's refusal surfaced.

    The ack carries `error` for a request the peer understood and declined;
    an exception means it never got there. Both end here as a refusal the
    operator can read, and neither leaves this node's state changed.
    """
    try:
        ack = await _client.get_instance().send_engine_control(
            engine, action, **kwargs)
    except Exception as exc:
        log.warning("[remote_control] %s/%s did not reach the peer: %s",
                    engine, action, exc)
        raise RemoteControlFailed(
            f"The remote node could not be reached ({exc}). Nothing changed."
        ) from exc
    if (ack or {}).get("error"):
        raise RemoteControlFailed(f"The remote node refused: {ack['error']}")
    return ack or {}


def _note(key: str, value) -> None:
    try:
        _client.get_instance().remote_settings[key] = value
    except Exception as exc:                      # pragma: no cover - defensive
        log.debug("[remote_control] could not record %s in the snapshot: %s",
                  key, exc)
