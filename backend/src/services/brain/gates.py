"""Which gate decided an entry, from the reason the app recorded (docs/todo/008).

The gates are listed in the order a signal meets them on the way to the
broker, which is the order the brain draws them. `classify` maps a recorded
reason onto one of them. The rules are ordered: "Trading paused ... Daily
loss limit" must hit the halt before anything else, and "Auto-execution
skipped -- Trading Schedule: ..." must not stop at "auto" just because it
starts with the word.

A reason no rule knows is `other`, never a guess: the picture is only worth
looking at if every pulse stops where the app really stopped it.
"""
from __future__ import annotations

GATES: list[dict] = [
    {"key": "auto", "label": "Auto-execute"},
    {"key": "node", "label": "Active node"},
    {"key": "halt", "label": "Pause & halts"},
    {"key": "breaker", "label": "Circuit breaker"},
    {"key": "schedule", "label": "Schedule & session"},
    {"key": "loss_cap", "label": "Channel loss cap"},
    {"key": "news", "label": "News"},
    {"key": "risk", "label": "Risk checks"},
    {"key": "model", "label": "Strategy filters"},
    {"key": "entry", "label": "Entry price"},
    {"key": "slots", "label": "Open-trade slots"},
    {"key": "other", "label": "Other"},
    {"key": "broker", "label": "Broker"},
]

# (gate, substrings) -- first match wins, so order is load-bearing.
_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("auto", ("auto-execution is off", "live_off", "live_disabled")),
    ("node", ("stood down", "active trader")),
    ("halt", ("paused", "daily loss limit", "halt", "give-back", "giveback")),
    ("breaker", ("circuit breaker",)),
    ("loss_cap", ("daily loss cap",)),
    # An Auto stand-down names "(Trading > Schedule: Auto)" as where to change
    # it; it is a strategy decision, not the schedule, so it goes first.
    ("model", ("auto:",)),
    ("schedule", ("schedule", "profit target", "for this window", "session")),
    ("news", ("news", "event tier", "blackout")),
    ("risk", ("exposure_guard", "r:r filter", "sig guard", "directional cap",
              "stop width", "max risk")),
    ("model", ("ml_skipped", "momentum", "bias", "unproven_edge", "meta_", "ml gate")),
    ("slots", ("max open trades",)),
    ("entry", ("breached", "entry zone", "stale", "queued", "fill delay",
               "filled_too_soon")),
    ("broker", ("ea rejected", "invalid volume", "not enough money", "rejected",
                "failed", "error", "no live price", "healthy ea")),
]


def rule_gates() -> list[str]:
    return [gate for gate, _ in _RULES]


def classify(reason, executed) -> tuple[str, str]:
    """(outcome, gate). outcome is executed | queued | blocked."""
    text = (reason or "").strip().lower()
    if not text:
        return ("executed", "broker") if executed else ("blocked", "other")
    for gate, needles in _RULES:
        if any(n in text for n in needles):
            outcome = "queued" if text.startswith("signal queued") else "blocked"
            return outcome, gate
    return "blocked", "other"
