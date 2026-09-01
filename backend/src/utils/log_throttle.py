"""Say it once, keep saying it occasionally, never say it 7,200 times.

Three loops in this app log the same line on every cycle while a condition
persists, and all three were found on 2026-09-01:

  * the reconciliation pass, every ~12s while a placeholder sat out its
    24-hour expiry -- roughly 7,200 identical warnings
  * `monitor_loop`'s "EA unhealthy", every second while the EA was off the
    chart -- ~400 lines during one seven-minute demo
  * the refused-close ERROR, every second for as long as AutoTrading was off

The cost is never disk. A warning that appears thousands of times stops being
read, and the next genuinely new one scrolls past inside it. Going silent is
worse: a condition that persists all day matters more than one that appears
once, not less.

So: loud on a change, quiet on a repeat, and a periodic reminder so a standing
problem cannot disappear.

Deliberately not a logging.Filter. The callers want to *choose* -- warn on a
change, drop to debug on a repeat -- and keep their own wording and level. A
filter can only drop records, which loses the quiet copy entirely.
"""
from __future__ import annotations

import time
from typing import Optional

# An hour. At one call a second, anything shorter barely reduces the noise;
# this turns 86,400 lines a day into 24.
DEFAULT_INTERVAL_S = 3600.0

# Keys can carry trade ids, and these loops run for weeks. Bounded so the
# store cannot become a slow leak; oldest entries go first, which at worst
# means a very old condition is announced once more than necessary.
MAX_TRACKED = 512

# key -> (signature, last announced at)
_seen: dict[str, tuple[str, float]] = {}


def should_announce(key: str, signature: str,
                    interval_s: Optional[float] = None) -> bool:
    """Should this be logged loudly right now?

    `key` is the subject -- a trade id, a host, a check name. `signature` is
    what is true about it: change the signature and it is announced again,
    because a different fact about the same subject is news.
    """
    interval = DEFAULT_INTERVAL_S if interval_s is None else interval_s
    now = time.time()
    previous = _seen.get(key)

    if previous is not None and previous[0] == signature:
        if (now - previous[1]) < interval:
            return False

    if key not in _seen and len(_seen) >= MAX_TRACKED:
        # Oldest by last-announced. Cheap, and this only runs when full.
        oldest = min(_seen, key=lambda k: _seen[k][1])
        _seen.pop(oldest, None)

    _seen[key] = (signature, now)
    return True


def clear(key: str) -> None:
    """Forget a subject, so its condition returning is announced afresh.

    Call when the condition resolves. Without it, a problem that comes back
    after being fixed matches the stale entry and is logged quietly.
    """
    _seen.pop(key, None)


def reset() -> None:
    """Forget everything. For tests, and for a deliberate re-announce."""
    _seen.clear()
