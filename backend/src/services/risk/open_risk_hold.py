"""Is the open-risk daily limit refusing orders right now? For the header badge.

Owner, 2026-11-11: "its still saying VPS: Trading Active when its actually
blocked". With `daily_limit_counts_open_risk` on, `position_guards` refuses an
order when today's closed P&L, minus open risk, minus the new order's risk,
would pass the daily loss limit. The closed-only halt
(`governor.apply_daily_loss_halt_on_close`) has not fired at that point, so
nothing is paused and the badge read "Trading Active" while 17 orders were
refused (-$31.29 closed against -$39.29, each order risking $11-15).

The badge cannot know the next order's risk, so the guard records the one it
refused (`remember`) and `state` re-asks the guard's own question about that
order with today's numbers. It answers None, which is not holding anything, as soon as
the trading day ends, a close frees enough room, or the switch or the limit
changes. The record is kept in `app_config` so a restart does not lose it.

Display only. Nothing here decides whether an order is placed: the guard's
refusal does not depend on this module, and a failure here never changes it
(`position_guards._open_risk_reason`).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

CONFIG_KEY = "open_risk_refusal"


def remember(*, day_base: float, new_risk: float) -> None:
    """Record the order the guard has just refused."""
    from backend.src.services.risk import governor
    from backend.src.services.risk.app_config_repo import set_app_config
    set_app_config(CONFIG_KEY, json.dumps({
        "day_start": governor.calendar_day_start_ts(),
        "day_base": float(day_base),
        "new_risk": float(new_risk),
        "ts": time.time(),
    }))


def state() -> Optional[dict]:
    """The hold, if the last refused order would still be refused; else None."""
    from backend.src.services.risk import governor
    from backend.src.services.risk import position_guards
    from backend.src.services.risk import repo as risk_repo
    from backend.src.services.risk.app_config_repo import get_app_config
    from backend.src.services.risk.risk_settings_repo import get_risk_settings

    raw = get_app_config(CONFIG_KEY)
    if not raw:
        return None
    rec = json.loads(raw)
    if abs(float(rec["day_start"]) - governor.calendar_day_start_ts()) > 1.0:
        return None
    rs = get_risk_settings()
    pct = float(rs.get("max_daily_loss_pct", 0) or 0)
    if not rs.get("daily_limit_counts_open_risk") or pct <= 0:
        return None

    day_base = float(rec["day_base"])
    new_risk = float(rec["new_risk"])
    limit = day_base * (pct / 100.0)
    realised = governor.daily_loss_realised()
    open_risk = sum(position_guards.position_risk(p)
                    for p in risk_repo.open_positions_for_guards())
    room = limit + realised - open_risk
    # The guard allows an order when realised - open_risk - new_risk > -limit.
    if room > new_risk:
        return None
    return {
        "room": max(room, 0.0), "limit": limit, "pct": pct, "day_base": day_base,
        "realised": realised, "open_risk": open_risk, "new_risk": new_risk,
        "ts": float(rec.get("ts") or 0),
    }


def describe(hold: dict) -> str:
    when = datetime.fromtimestamp(hold["ts"], tz=timezone.utc).strftime("%H:%M UTC")
    return (f"Orders that risk more than ${hold['room']:.2f} are refused: "
            f"${hold['realised']:.2f} closed today and ${hold['open_risk']:.2f} at risk "
            f"in open trades, against the -${hold['limit']:.2f} daily loss limit "
            f"({hold['pct']:.1f}% of ${hold['day_base']:,.2f}). The last refused order "
            f"risked ${hold['new_risk']:.2f} at {when}. This lifts when the trading "
            f"day ends, or when a close frees enough room.")
