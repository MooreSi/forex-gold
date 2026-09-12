"""Does this engine's silence need saying out loud?

An engine that has found nothing looks exactly like an engine that is refusing
everything: both produce no signals, and the status line says "running" either
way. On 2026-09-12 the difference turned out to be sixteen days and 514 refused
candidates (`docs/simon-handover/034`), and it was found by reading the
database rather than by anything the app said.

The Bounce engine has no panel — it was removed on 2026-09-02 — so the log is
the only place this can surface. Kept separate from the watchdog loop that
emits it so the decision can be tested without an engine, a database or a
clock: `tests/test_signal/test_silence_warning.py`.
"""
from __future__ import annotations

from typing import Optional

# Two days. Below this, silence is a quiet market: this engine has gone a
# weekend without a signal many times. A warning that fires on an ordinary
# quiet spell is one the reader learns to skip, which is how the stale-EA
# warning stopped working (bugs/033).
DEFAULT_MIN_SILENT_SECS = 48 * 3600


def format_silence_warning(report: dict, *,
                           min_silent_secs: float = DEFAULT_MIN_SILENT_SECS
                           ) -> Optional[str]:
    """A line worth logging, or None when there is nothing to say.

    Silent on three cases, each for its own reason:

    * **under the threshold** — an ordinary quiet spell;
    * **never produced a signal** — a fresh install is not a fault, and this
      would otherwise warn on first run on every machine, forever;
    * **silent with nothing refused** — the engine found nothing to judge, so
      the market is the explanation and naming a gate would point at one that
      did nothing.

    The commonest refusal is in the line deliberately. "No signal in 16 days"
    sends the reader to look at the market; "369 of them by the ML gate" sends
    them to the ML gate, which is where the answer is.
    """
    silent = report.get("silent_secs")
    if silent is None or silent <= min_silent_secs:
        return None
    refusals = report.get("refusals") or []
    if not refusals:
        return None
    top_name, top_n = refusals[0]
    return (
        f"no signal in {int(silent // 86400)} days — "
        f"{report.get('candidates', 0)} candidates found and every one refused, "
        f"{top_n} by {top_name}"
    )
