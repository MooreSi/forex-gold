"""A channel's re-posted signal replaces the one it corrects.

Live 2026-10-02, Gold Diggers VIP: "ENTRY 4180-4176 SL 4174" and, 18 seconds
later, "ENTRY 4180-4178 SL 4177". Price was above both zones, both were
queued, and an EA template's queued signal waits up to an hour -- so both
would have filled on the return to 4180, two positions for one setup.

Owner, 2026-10-02: within REPOST_WINDOW_SECS the newer post replaces the
older one -- same channel, same direction (the app trades XAUUSD only, so
that is the same symbol too). Outside the window both stand.

Only a still-queued signal with no working EA order is cancelled, because
that costs nothing: a filled one is a position (close_trade is frozen) and a
resting EA order needs a broker call to withdraw. And only once the newer
post has itself become a signal -- a skipped correction replaces nothing.

Pinned by tests/signals/test_a_reposted_signal_replaces_the_queued_one.py.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from backend.src.services.signals import repo as signals_repo
from backend.src.services.telegram import alerts as telegram_alerts

log = logging.getLogger(__name__)

REPOST_WINDOW_SECS = 120.0


async def supersede_earlier_pending(
    tg_id: str, source_label: str, direction: str, now: Optional[float] = None,
) -> list[str]:
    """Cancel what `tg_id`'s signal replaces; returns the cancelled ids."""
    now = time.time() if now is None else now
    ids = signals_repo.cancel_pending_reposts(
        tg_id, f"Telegram Auto ({source_label})", direction,
        now - REPOST_WINDOW_SECS, now,
    )
    if ids:
        log.info("[%s] tg_id=%s %s replaces queued signal(s) %s posted within %.0fs",
                 source_label, tg_id, direction, [i[:8] for i in ids], REPOST_WINDOW_SECS)
        asyncio.create_task(telegram_alerts.send_message(
            f"*Signal replaced* — {source_label}\n"
            f"A corrected {direction} was posted within {REPOST_WINDOW_SECS:.0f}s. "
            f"The earlier queued signal is cancelled; only the latest one stays queued.",
            tg_id, "tg_signal_replaced",
        ))
    return ids
