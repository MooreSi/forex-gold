"""The brain view's data: recent entry decisions and the gates' state now
(docs/todo/008).

Read-only. Nothing here writes to a database, changes a setting or reaches a
broker; it reads what the order paths already record and what the gates
already report. Every read is guarded on its own, so one log or one gate that
cannot be read costs itself, not the picture.

A gate state that cannot be read is `None` (unknown), never `False`: an
unreadable circuit breaker is not a breaker that is off.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from backend.src.services.brain import feed_repo
from backend.src.services.brain import gates as _gates

log = logging.getLogger(__name__)

_ENGINE_OK = {"executed", "success"}   # Reversal writes one, Breakout the other
_PREFIX = {"reversal": "re", "breakout": "bo"}

# The engines record a code, not a sentence. Plain words for the ones they
# really write; anything else is shown as recorded.
_ENGINE_WORDS = {
    "ml_skipped": "the ML model scored it below its floor",
    "momentum_skipped": "M5 momentum was against it",
    "bias_skipped": "against the higher-timeframe trend",
    "filled_too_soon": "price reached the zone too soon after the signal",
    "skipped:exposure_guard": "too much exposure in that direction already",
    "skipped:schedule": "outside its schedule window",
    "skipped:news": "news blackout",
    "skipped:unproven_edge": "no proven edge yet",
    "skipped:live_off": "live execution is off",
    "skipped:live_disabled": "live execution is off",
}


def _engine_event(kind: str, source: str, row: dict) -> dict:
    status = str(row.get("status") or "")
    if status in _ENGINE_OK:
        outcome, gate, reason = "executed", "broker", ""
    else:
        reason = (status.split(":", 1)[1] if status.startswith(("error:", "failed:"))
                  else _ENGINE_WORDS.get(status, status))
        outcome, gate = _gates.classify(status, False)
    return {"key": f"{_PREFIX[kind]}:{row['id']}", "ts": float(row["ts"] or 0), "kind": kind,
            "source": source, "direction": row.get("direction") or "",
            "outcome": outcome, "gate": gate, "reason": reason}


def _telegram_event(row: dict) -> dict:
    outcome, gate = _gates.classify(row.get("reason"), bool(row.get("executed")))
    return {"key": f"tg:{row['id']}", "ts": float(row["ts"] or 0), "kind": "telegram",
            "source": row.get("source") or "Telegram", "direction": row.get("direction") or "",
            "outcome": outcome, "gate": gate, "reason": row.get("reason") or ""}


def _read(fn: Callable[[int], list], limit: int) -> list:
    try:
        return fn(limit)
    except Exception as exc:
        log.debug("[Brain] a decision log is unreadable: %s", exc)
        return []


def events(limit: int = 60) -> list[dict]:
    """The most recent decisions across every source, newest first."""
    out = [_telegram_event(r) for r in _read(feed_repo.recent_telegram, limit)]
    out += [_engine_event("reversal", "Reversal Engine", r)
            for r in _read(feed_repo.recent_reversal, limit)]
    out += [_engine_event("breakout", "Breakout Engine", r)
            for r in _read(feed_repo.recent_breakout, limit)]
    out.sort(key=lambda e: e["ts"], reverse=True)
    return out[:limit]


# ── Gate states: (blocking, detail) each ────────────────────────────────────

def _rs() -> dict:
    from backend.src.db import database as db_module
    return db_module.get_risk_settings() or {}


def _auto() -> tuple[bool, str]:
    on = bool(_rs().get("auto_execute_signals", 0))
    return (not on, "" if on else "Telegram auto-execution is off")


def _halt() -> tuple[bool, str]:
    from backend.src.services.risk.governor import halt_reason, is_trading_paused
    paused = is_trading_paused()
    return paused, (halt_reason() or "Trading paused") if paused else ""


def _breaker() -> tuple[bool, str]:
    from backend.src.services.risk.circuit_breaker_repo import get_circuit_breaker_state
    cb = get_circuit_breaker_state()
    return bool(cb.get("is_active")), ""


def _schedule() -> tuple[bool, str]:
    from backend.src.db import database as db_module
    from backend.src.services.risk.schedule import check_trading_schedule
    ok, name = db_module.is_session_allowed(_rs())
    if not ok:
        return True, f"Session '{name}' is switched off"
    allowed, reason = check_trading_schedule()
    return (not allowed), reason


def _loss_cap() -> tuple[bool, str]:
    from backend.src.services.risk import channel_loss_cap
    held = [r["channel"] for r in channel_loss_cap.state()["channels"] if r["held"]]
    return bool(held), ", ".join(held)


def _news() -> tuple[bool, str]:
    from backend.src.utils.news_calendar import check_news_blackout
    allowed, reason = check_news_blackout()
    return (not allowed), reason


def _slots() -> tuple[bool, str]:
    from backend.src.services.trading import signal_state_repo
    used = signal_state_repo.count_trade_slots_used()
    cap = int(_rs().get("max_open_trades", 1) or 1)
    return used >= cap, f"{used} of {cap} in use"


_STATE_READERS: dict[str, str] = {
    "auto": "_auto", "halt": "_halt", "breaker": "_breaker", "schedule": "_schedule",
    "loss_cap": "_loss_cap", "news": "_news", "slots": "_slots",
}


def gate_states() -> list[dict]:
    out = []
    for g in _gates.GATES:
        blocking: Optional[bool] = None
        detail = ""
        reader = _STATE_READERS.get(g["key"])
        if reader:
            try:
                blocking, detail = globals()[reader]()
            except Exception as exc:
                log.debug("[Brain] gate %s unreadable: %s", g["key"], exc)
        out.append({**g, "blocking": blocking, "detail": detail or ""})
    return out


def snapshot(limit: int = 60) -> dict:
    return {"now": time.time(), "gates": gate_states(), "events": events(limit)}


async def snapshot_async(limit: int = 60) -> dict:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(snapshot, limit)
