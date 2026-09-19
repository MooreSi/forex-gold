"""Stopping the app from the dashboard.

The NiceGUI header's power button offered two things: Restart and Stop. The
React port kept restart -- `POST /api/node/restart`, through
`remote_node_controller` -- and lost stop, so the only way to shut the app
down was to kill the process. That skips the one piece of bookkeeping the
restart path is careful about: persisting the Telegram bot's update offset, so
the next start does not replay the command that caused the shutdown.

**The stop is scheduled, not awaited.** The server this runs inside is the
server carrying the response; shutting it down synchronously kills the
connection mid-reply and the dashboard reports a network error for an action
that worked perfectly.

Nothing here closes a position or touches the broker. Engines stop because the
process stops, and any open position keeps running to its own SL/TP on the
broker's side -- the same thing that happens when the machine is turned off.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.src.services.risk.app_config import set as _set_app_config
from backend.src.utils.os_utils import shutdown_ui as _shutdown_ui

log = logging.getLogger(__name__)

__all__ = ["stop_app"]

# Long enough for the HTTP response to reach the browser and for the operator
# to see the confirmation, short enough that "stopping" is not a state anyone
# sits in. The restart path uses five for the same reason.
_DEFAULT_DELAY_SECS = 3


async def _stop_after(delay_secs: int) -> None:
    await asyncio.sleep(delay_secs)
    _shutdown_ui()


async def stop_app(engine: Any, delay_secs: int = _DEFAULT_DELAY_SECS) -> str:
    """Shut the app down. Returns the line the dashboard shows."""
    offset = getattr(engine, "_bot_offset", None)
    if offset is not None:
        try:
            _set_app_config("bot_update_offset", str(offset))
        except Exception as exc:
            # The operator pressed stop. A failed bookkeeping write is a worse
            # reason to stay running than replaying one Telegram update is to
            # stop.
            log.warning("[power] could not persist the bot offset: %s", exc)

    asyncio.create_task(_stop_after(delay_secs))
    return (
        "Stopping FOREX Trader. It will not come back on its own — start it "
        "again from the desktop shortcut or run.py."
    )
