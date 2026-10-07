"""Which trading session a UTC hour belongs to.

Moved out of the Bounce engine when that engine was removed (2026-09-14). It
was never Bounce-specific: the **Breakout** engine imports all three of these,
and did so while they lived in another engine's package.

**`session_quality` no longer reads an adaptive parameter.** It used to ask the
Bounce engine's `allow_asian`, which is `docs/todo/bugs/045` — a parameter its
own engine never consulted, tuned by an AI that believed it was switching the
Asian session off, and read only here. That store is gone with the engine, so
the value it held in practice is written down instead: **`allow_asian` was
0.0**, which made the Asian session "low", and that is preserved exactly.

Changing it is a one-word edit and a decision, not a tuning knob. Note also
that the only live caller, `breakout_signal_velocity`, refuses the Asian
session unconditionally on the line after it asks — so today this grading
changes no behaviour either way (bugs/045).

The hour-to-session mapping is `utils/sessions.session_for_hour`, the one
definition the whole app uses (bugs/057, owner 2026-10-07). This module adds
only the weekend.
"""
from __future__ import annotations

from datetime import datetime, timezone

from backend.src.utils.sessions import session_for_hour

# Frozen from the Bounce engine's stored `allow_asian = 0.0`. See the module
# docstring: this is a recorded decision now, not a parameter.
ASIAN_SESSION_QUALITY = "low"


def get_session() -> str:
    now = datetime.now(timezone.utc)
    dow = now.weekday()  # 0=Mon … 6=Sun
    h   = now.hour
    # XAUUSD is closed Saturday all day and Sunday until ~22:00 UTC
    if dow == 5:           # Saturday — fully closed
        return "closed"
    if dow == 6 and h < 22:  # Sunday before Asian open
        return "closed"
    return session_for_hour(h)   # the one definition (bugs/057)


def session_quality(session: str) -> str:
    return {
        "overlap": "high",
        "london":  "good",
        "ny":      "good",
        "asian":   ASIAN_SESSION_QUALITY,
        "off":     "low",
        "closed":  "closed",  # weekend / market-closed — never trade
    }.get(session, "low")


def session_is_active(session: str) -> bool:
    return session_quality(session) in ("high", "good")


# ── HTF bias ──────────────────────────────────────────────────────────────────
