"""Two sources disagree about direction. What should happen?

Stage 1 of contradiction handling. **Nothing here is wired to execution.**
`contradiction_log` calls it to record what each policy WOULD have done, the
way `decision_shadow` does for the four off gates, so the question can be
answered with numbers from this account before any money moves. Promoting a
policy to the live path needs the owner and a demo session.

WHY IT IS A PURE FUNCTION
-------------------------
It decides about order placement. Everything it needs is an argument --
including `now` -- so every branch is a sentence a test can assert, and a
future live caller cannot change the answer by changing what it reads.

THE THREE RULES THAT ARE NOT OBVIOUS
------------------------------------
**A source never contradicts itself.** A channel that flips direction is
correcting itself, and `scan_edit_reparse.py` already owns that case (it can
close a real position). An engine holding both sides is a position-level
question `positions/core_internal_exposure_guard.py` already answers -- and
answers OFF by default, on measured evidence: opposing reversal-engine legs
were 19% of closed trades and roughly 80% of the profit. Folding either case
in here would re-fight a decision that has already been made with data.

**Cancelling is free; closing is not.** A signal that has not filled can be
withdrawn for nothing. One that has is a live position, and `close_trade` is
frozen. So SUPERSEDE names only signals still waiting, and when an opposing
signal has already filled the verdict falls back to BLOCK rather than
reaching for the close path. `test_no_shipped_policy_can_close_a_live_
position` asserts that over the whole policy set, not per mode.

**An unavailable fact abstains.** `Active.is_pending` may be None, and the
one mode that reads it then returns UNKNOWN rather than an answer. Same rule
as `decision_shadow.decide`, for the same reason: a policy that "decided"
on a fact it never had would report a refusal rate that says nothing about
the policy, and would be read as though it did. The bus does not yet record
fill state, so today every entry arrives with None and `freshest wins`
abstains on every contradiction -- visibly, as a NULL, rather than by
quietly agreeing with whichever mode happens to be next to it.

**Each source-kind pair is its own question.** engine/engine,
engine/telegram and telegram/telegram have different right answers -- the
first is already suppressed on the bus, the last has never been looked at.
A policy names the pairs it governs; a single global switch would get at
least one of the three wrong.

Unknown input fails OPEN. An unknown mode, an unknown symbol on the
candidate's own side, a policy with no pairs: all allow. This function's
whole job is to describe a restriction, and a restriction nobody asked for
is the expensive direction of wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Source kinds. These are the bus's own, imported by value rather than from
# signal_bus_repo: this module is pure, and a data-layer import would make
# it the one thing it must not be.
KIND_ENGINE   = "engine"
KIND_TELEGRAM = "telegram"

PAIR_ENGINE_ENGINE     = "engine|engine"
PAIR_ENGINE_TELEGRAM   = "engine|telegram"
PAIR_TELEGRAM_TELEGRAM = "telegram|telegram"
ALL_PAIRS: tuple[str, ...] = (
    PAIR_ENGINE_ENGINE, PAIR_ENGINE_TELEGRAM, PAIR_TELEGRAM_TELEGRAM,
)

MODE_OFF           = "off"
MODE_FIRST_WINS    = "first_wins"
MODE_FRESHEST_WINS = "freshest_wins"
MODE_SHRINK        = "shrink"

ALLOW     = "allow"
BLOCK     = "block"
SHRINK    = "shrink"
SUPERSEDE = "supersede"
# Not a decision. The policy had no fact to decide on -- see the module
# docstring. Stored as NULL, never as a refusal.
UNKNOWN   = "unknown"


def pair_key(kind_a: str, kind_b: str) -> str:
    """Order-independent name for a pair of source kinds."""
    a, b = sorted(((kind_a or "").strip().lower(), (kind_b or "").strip().lower()))
    return f"{a}|{b}"


@dataclass(frozen=True)
class Candidate:
    """The signal being decided about."""
    source_kind: str
    source_name: str
    direction: str
    symbol: str = ""
    at: float = 0.0


@dataclass(frozen=True)
class Active:
    """Something already on the bus that the candidate might contradict.

    `is_pending` is the expensive field. True means the signal has not
    filled and can be withdrawn; False means there is a position behind it;
    **None means nobody knows**, which is where the bus is today.

    Defaulting it to False would be the safe-looking choice and the wrong
    one -- it would make every unknown row uncancellable and turn
    freshest-wins into first-wins without saying so. It has no default at
    all, so a caller has to state which of the three it means.
    """
    source_kind: str
    source_name: str
    direction: str
    symbol: str
    created_at: float
    is_pending: Optional[bool]
    signal_ref: str = ""


@dataclass(frozen=True)
class Policy:
    name: str
    mode: str
    window_secs: float = 600.0
    lot_mult: float = 1.0
    pairs: tuple[str, ...] = ALL_PAIRS
    is_champion: bool = False


@dataclass(frozen=True)
class Verdict:
    action: str
    reason: str = ""
    lot_mult: float = 1.0
    supersede: tuple[str, ...] = ()
    opposing: tuple[str, ...] = field(default=())


# The shadow set. One champion (what the live path does now: nothing) and
# four challengers. `shrink` is here because the internal-exposure
# measurement says opposing legs are where the profit came from -- a mode
# that keeps both sides is the one that respects that evidence instead of
# overriding it, and the study exists to find out whether it survives.
POLICIES: tuple[Policy, ...] = (
    Policy("live (champion)", MODE_OFF, is_champion=True),
    Policy("first wins", MODE_FIRST_WINS, window_secs=600.0),
    Policy("first wins (channels only)", MODE_FIRST_WINS, window_secs=600.0,
           pairs=(PAIR_TELEGRAM_TELEGRAM,)),
    Policy("freshest wins", MODE_FRESHEST_WINS, window_secs=600.0),
    Policy("half size", MODE_SHRINK, window_secs=600.0, lot_mult=0.5),
)


def _same_source(a: str, b: str) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def _same_symbol(a: str, b: str) -> bool:
    """Unknown on either side matches. Only rows written before the bus had
    a symbol column are unknown, and an instrument nobody recorded cannot be
    ruled out."""
    sa, sb = (a or "").strip().upper(), (b or "").strip().upper()
    return not sa or not sb or sa == sb


def opposing_entries(candidate: Candidate, active: list[Active],
                     policy: Policy, now: float) -> list[Active]:
    """The subset of `active` that genuinely contradicts the candidate."""
    direction = (candidate.direction or "").strip().upper()
    out: list[Active] = []
    for a in active:
        if (a.direction or "").strip().upper() == direction:
            continue
        if _same_source(a.source_name, candidate.source_name):
            continue
        if not _same_symbol(a.symbol, candidate.symbol):
            continue
        if policy.window_secs > 0 and (now - float(a.created_at)) >= policy.window_secs:
            continue
        if pair_key(candidate.source_kind, a.source_kind) not in policy.pairs:
            continue
        out.append(a)
    return out


def resolve(candidate: Candidate, active: list[Active], policy: Policy,
            now: Optional[float] = None) -> Verdict:
    """What `policy` says about `candidate` given what is already live."""
    if policy.mode == MODE_OFF:
        return Verdict(ALLOW, "contradiction handling is off")

    now = float(now if now is not None else candidate.at)
    opposing = opposing_entries(candidate, active, policy, now)
    if not opposing:
        return Verdict(ALLOW, "no opposing signal")

    names = tuple(a.source_name for a in opposing)
    who = ", ".join(names)

    if policy.mode == MODE_FIRST_WINS:
        return Verdict(BLOCK, f"an opposing signal from {who} is already active",
                       opposing=names)

    if policy.mode == MODE_SHRINK:
        return Verdict(SHRINK, f"opposing signal from {who} — taken at reduced size",
                       lot_mult=policy.lot_mult, opposing=names)

    if policy.mode == MODE_FRESHEST_WINS:
        if any(a.is_pending is None for a in opposing):
            return Verdict(
                UNKNOWN,
                f"fill state is not recorded for the opposing signal from {who}",
                opposing=names,
            )
        filled = [a for a in opposing if not a.is_pending]
        if filled:
            return Verdict(
                BLOCK,
                f"an opposing signal from {', '.join(a.source_name for a in filled)} "
                "is already live — cancelling it would mean closing a position",
                opposing=names,
            )
        return Verdict(SUPERSEDE, f"cancels the opposing pending signal from {who}",
                       supersede=tuple(a.signal_ref for a in opposing if a.signal_ref),
                       opposing=names)

    return Verdict(ALLOW, f"unknown contradiction mode {policy.mode!r}")
