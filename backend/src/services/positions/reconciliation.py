"""Broker vs DB reconciliation (stage3/030) — the arbiter, report-only.

Broker and DB are dual-written with no arbiter. `open_trade` places the real
order and THEN inserts the row, so a crash in between leaves a live position
nothing is managing: no stop watched, no target, no harvest. The mirror gap
exists on close, and 020's parked `unknown` signals have no resolver at all.

This module is the diff engine: broker snapshot + DB snapshot in, typed
differences out. It is a PURE FUNCTION with no I/O, deliberately -- it is the
part where a mistake is expensive and a test is cheap, and keeping the broker
out of its signature is what makes the read-only guarantee structural rather
than a promise.

READ-ONLY AT THE BROKER, ALWAYS. An arbiter that can place or close orders is
just another writer. `diff_snapshots` is never handed a bridge, and a test
asserts this module names no order-writing function at all.

REPORT-ONLY IS THE SHIPPED DEFAULT, confirmed by Simon in
docs/simon-handover/001-trading-defaults.md: "report-only for the first week,
then switch to repair". Nothing here writes to either side. The repairers are
the second half of 030 and are not built yet.

Not to be confused with `core_template_placeholder_repair`, which adopts or
closes EA-template placeholder rows from broker records. That runs on its own
and is narrower; this pass reports on everything, including the rows that one
deliberately leaves alone.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

from backend.src.utils import log_throttle as _throttle


# Kinds, in the order a reader should care about them.
# A broker position with no DB row is TWO situations wearing one shape, and
# Simon's answer to 001-trading-defaults #6 (25 Aug, after this spec was
# written) treats them oppositely: "Watch it only... Manual MT5 trades stay
# Simon's; the app still counts them toward exposure and the risk limits, but
# never moves a stop or closes one."
#
# stage3/010 made them separable -- every order the app sends carries "ea:" or
# "py:" plus the trade id, and a position with neither is not ours. Collapsing
# them would either abandon the app's own crash orphans or take over Simon's
# manual trades.
BROKER_ONLY_OURS = "broker_only_ours"        # we placed it, then lost the row
BROKER_ONLY_MANUAL = "broker_only_manual"    # not ours -- watch, never touch
DB_ONLY_CLOSED = "db_only_closed"            # gone from the broker, deal explains it
DB_ONLY_NO_EVIDENCE = "db_only_no_evidence"  # gone, and nothing explains it
UNKNOWN_FILLED = "unknown_filled"            # a parked signal that did fill
UNKNOWN_NOT_FILLED = "unknown_not_filled"    # a parked signal that did not
MATCHED = "matched"

# Everything except MATCHED wants a human to look, at least while this is
# report-only.
# Shared with broker/dedup.py's lookback on purpose -- the spec asks for one
# window, not two that can drift apart.
_DEAL_DAYS = 1

# Watch-only does not mean ignore: a manual position still counts toward
# exposure and the risk limits, which is exactly why the report must show it.
_ATTENTION_KINDS = frozenset({
    BROKER_ONLY_OURS, BROKER_ONLY_MANUAL, DB_ONLY_CLOSED, DB_ONLY_NO_EVIDENCE,
    UNKNOWN_FILLED, UNKNOWN_NOT_FILLED,
})


@dataclass(frozen=True)
class DiffEntry:
    kind: str
    trade_id: Optional[str] = None
    signal_id: Optional[str] = None
    ticket: Optional[int] = None
    entry_price: float = 0.0
    close_price: float = 0.0
    profit: float = 0.0
    detail: str = ""


@dataclass(frozen=True)
class ReconcileDiff:
    entries: list = field(default_factory=list)

    def of_kind(self, kind: str) -> list:
        return [e for e in self.entries if e.kind == kind]

    @property
    def needs_attention(self) -> bool:
        return any(e.kind in _ATTENTION_KINDS for e in self.entries)


def _id_prefixes(trade_id: str) -> tuple[str, ...]:
    """The comment prefixes a broker record could carry for this trade.

    Shared vocabulary with broker/dedup.py so one trade is recognised the same
    way wherever it is looked for -- the EA writes "ea:", the Python bridge
    writes "py:" (stage3/010), and nothing else the broker writes matches
    either.
    """
    from backend.src.services.broker.dedup import _prefixes
    return _prefixes(trade_id)


def _comment_matches(comment: Any, prefixes: tuple[str, ...]) -> bool:
    text = str(comment or "")
    return bool(prefixes) and any(text.startswith(p) for p in prefixes)


def _is_ours(comment: Any) -> bool:
    """Did this app place the order that opened this position?

    Any trade id, not a particular one: the question here is ownership, not
    which trade. "ea:" is the EA's, "py:" is the Python bridge's (stage3/010).
    Everything else -- a blank comment, MT5's own "[sl 4046.50]" or
    "batchClose", or whatever a human typed -- is not ours.
    """
    from backend.src.services.broker.dedup import BRIDGE_COMMENT_PREFIX
    text = str(comment or "")
    return text.startswith("ea:") or text.startswith(BRIDGE_COMMENT_PREFIX)


def _closing_deals(deals: Iterable[dict], ticket: Optional[int]) -> list:
    """Deals that closed the position with this ticket.

    entry != 0 is an exit. An opening deal is not evidence of a close -- read
    as one it would record the trade shut at its own entry price.
    """
    if not ticket:
        return []
    return [d for d in deals
            if int(d.get("position_id", 0) or 0) == int(ticket)
            and int(d.get("entry", 0) or 0) != 0]


def diff_snapshots(broker_positions: Iterable[dict],
                   broker_deals: Iterable[dict],
                   db_open_trades: Iterable[dict],
                   unknown_signals: Iterable[dict] = ()) -> ReconcileDiff:
    """Compare what the broker has against what the database believes.

    No I/O and no bridge: hand it snapshots, get differences. Nothing is
    repaired here -- that is the caller's decision and, for now, nobody's.
    """
    positions = list(broker_positions or [])
    deals = list(broker_deals or [])
    # Closed and cancelled rows are history, not a reconciliation target.
    db_rows = [t for t in (db_open_trades or []) if t.get("status") == "open"]

    entries: list = []
    claimed_tickets: set = set()

    for row in db_rows:
        trade_id = row.get("trade_id")
        ticket = int(row.get("mt5_ticket") or 0) or None
        prefixes = _id_prefixes(trade_id or "")

        match = None
        if ticket is not None:
            match = next((p for p in positions
                          if int(p.get("ticket") or 0) == ticket), None)
        if match is None:
            # A row with no ticket yet -- an EA template placeholder waiting on
            # a leg fill -- is still linked to its legs by the order comment,
            # the only link that survives into MT5's own records.
            match = next((p for p in positions
                          if _comment_matches(p.get("comment"), prefixes)), None)

        if match is not None:
            claimed_tickets.add(int(match.get("ticket") or 0))
            entries.append(DiffEntry(
                kind=MATCHED, trade_id=trade_id,
                ticket=int(match.get("ticket") or 0) or None,
                entry_price=float(match.get("open_price") or 0),
                detail="open at the broker and in the database"))
            continue

        closers = _closing_deals(deals, ticket)
        if closers:
            last = max(closers, key=lambda d: d.get("time", 0))
            profit = round(sum(
                float(d.get("profit", 0) or 0) + float(d.get("swap", 0) or 0)
                + float(d.get("fee", 0) or 0) for d in closers), 2)
            entries.append(DiffEntry(
                kind=DB_ONLY_CLOSED, trade_id=trade_id, ticket=ticket,
                close_price=float(last.get("price") or 0), profit=profit,
                detail=f"closed at the broker across {len(closers)} deal(s)"))
            continue

        # No position and no closing deal. NOT proof it closed -- equally
        # consistent with a broker read that failed -- so it is flagged and
        # left open rather than booked shut on a guess.
        entries.append(DiffEntry(
            kind=DB_ONLY_NO_EVIDENCE, trade_id=trade_id, ticket=ticket,
            detail="open in the database, and the broker has no record of it"))

    for pos in positions:
        ticket = int(pos.get("ticket") or 0)
        if ticket in claimed_tickets:
            continue
        ours = _is_ours(pos.get("comment"))
        entries.append(DiffEntry(
            kind=BROKER_ONLY_OURS if ours else BROKER_ONLY_MANUAL,
            ticket=ticket or None,
            entry_price=float(pos.get("open_price") or 0),
            detail=(
                "we placed this and then lost its row — nothing is managing it"
                if ours else
                "not placed by this app (no order id in its comment) — counts "
                "toward exposure, but never touch it: no stop moved, no close")))

    for sig in (unknown_signals or []):
        trade_id = sig.get("trade_id") or ""
        prefixes = _id_prefixes(trade_id)
        filled = (
            any(_comment_matches(p.get("comment"), prefixes) for p in positions)
            or any(_comment_matches(d.get("comment"), prefixes) for d in deals)
        )
        entries.append(DiffEntry(
            kind=UNKNOWN_FILLED if filled else UNKNOWN_NOT_FILLED,
            trade_id=trade_id or None, signal_id=sig.get("signal_id"),
            detail=("the broker has this trade — the send did fill" if filled
                    else "the broker has no trace — the send did not fill")))

    return ReconcileDiff(entries=entries)


def _report_text(diff: ReconcileDiff) -> str:
    """The summary text, formatted once so the WARNING and the throttled DEBUG
    line cannot drift apart."""
    if not diff.needs_attention:
        return "Reconciliation: no differences between the broker and the database."

    lines = ["Reconciliation found differences:"]
    for kind in (BROKER_ONLY_OURS, BROKER_ONLY_MANUAL, DB_ONLY_CLOSED,
                 DB_ONLY_NO_EVIDENCE, UNKNOWN_FILLED, UNKNOWN_NOT_FILLED):
        found = diff.of_kind(kind)
        if not found:
            continue
        lines.append(f"  {kind}: {len(found)}")
        for e in found:
            lines.append(
                f"    trade={e.trade_id or '-'} signal={e.signal_id or '-'} "
                f"ticket={e.ticket or '-'} — {e.detail}")

    return "\n".join(lines)


def report(diff: ReconcileDiff) -> str:
    """A human-readable summary. Writes nothing; returns the text and logs it."""
    text = _report_text(diff)
    if diff.needs_attention:
        log.warning("[reconcile] %s", text)
    return text


# ── Reporting the same thing over and over ───────────────────────────────────
#
# Found live on the owner's demo account, 2026-09-01. Two EA-template
# placeholders whose legs never filled sat open for their designed 24-hour
# expiry, and this pass logged the identical two-line WARNING about them every
# TWELVE SECONDS -- the monitor loop fast-polls at 1s while trades are open and
# this runs every 12 cycles. Roughly 7,200 identical warnings over one row's
# life, into a log already 35MB.
#
# The cost is not disk. A warning that appears 7,200 times stops being read,
# and the next genuinely new difference scrolls past inside it -- which is the
# failure this pass exists to prevent.
#
# So: any CHANGE to the set is reported immediately and in full, an unchanged
# set drops to DEBUG, and a standing disagreement is still repeated at WARNING
# every _REPEAT_REMINDER_S so the throttle cannot turn into silence. A problem
# that persists for a day is worse than one that appears once, not better.

# Superseded 2026-09-01 by backend/src/utils/log_throttle, once this became
# the third site with the same problem. The bespoke version here was written
# first; keeping it would have left two implementations of one idea, which is
# what the duplicate-implementation detector exists to catch.
_THROTTLE_KEY = "reconcile"


def _diff_signature(diff: ReconcileDiff) -> tuple:
    """What counts as "the same disagreement".

    The whole entry, not just the trade id: the same trade moving from "no
    evidence" to "closed at the broker" is a different fact and must not be
    swallowed as a repeat. Sorted, because the broker's position list has no
    guaranteed order and an order-sensitive signature would never match twice
    -- which would throttle nothing at all.
    """
    return tuple(sorted(
        (e.kind, e.trade_id, e.signal_id, e.ticket, e.detail)
        for e in diff.entries if e.kind in _ATTENTION_KINDS
    ))


def reset_report_throttle() -> None:
    """Forget what was last reported. For tests, and for a deliberate re-report."""
    _throttle.clear(_THROTTLE_KEY)


def report_periodic(diff: ReconcileDiff) -> None:
    """report(), for the pass that runs every few seconds forever.

    `report()` itself is left alone: it returns text and is the one-shot
    surface, so throttling inside it would make a deliberate call silently do
    nothing.
    """
    if not diff.needs_attention:
        reset_report_throttle()
        return

    if _throttle.should_announce(_THROTTLE_KEY, repr(_diff_signature(diff))):
        report(diff)
        return

    # Quiet, not absent.
    log.debug("[reconcile] unchanged: %s", _report_text(diff))


# ── The periodic pass ────────────────────────────────────────────────────────
#
# Runs from the monitor cycle. Report-only: it reads the broker, reads the
# database, and logs what disagrees. It writes nothing to either side, which
# is why it is safe to run unattended and why it needed no startup-ordering
# change -- "reconcile before the monitor loop manages" is load-bearing for
# REPAIR, not for a report that cannot change anything.
#
# The cadence is a constant rather than an Expert Tunable. The spec asks for a
# tunable, and one should land with the repairers, when the interval starts to
# mean something. A dial that only changes how often a log line appears is not
# something a trader wants to move (docs/system/rules/60-adding-a-tunable.md:
# "expose a constant when a TRADER would want to move it").
_REPORT_EVERY_CYCLES = 12


async def collect_and_report(bridge: Any) -> Optional[ReconcileDiff]:
    """Snapshot both sides, diff them, log any disagreement. Never raises.

    Read-only throughout: `get_positions` / `get_deal_history` at the broker,
    plain SELECTs in the database. A failure to read either side means the
    comparison is meaningless, so it reports nothing rather than inventing a
    difference from half a picture -- an empty broker read would otherwise
    look exactly like every trade having vanished.
    """
    from backend.src.db import database as db_module
    from backend.src.services.analytics import reporting as _reporting
    from backend.src.services.trading import signal_state_repo as _signal_state_repo

    if bridge is None or not getattr(bridge, "is_configured", lambda: False)():
        return None
    try:
        positions = await bridge.get_positions()
        if positions is None:
            log.debug("[reconcile] skipped — the broker returned no position list")
            return None
        deals = await bridge.get_deal_history(_DEAL_DAYS) or []
    except Exception as e:
        log.debug("[reconcile] skipped — broker read failed: %s", e)
        return None

    # An abandoned activation claim is invisible to everything else -- the
    # scheduler only selects 'pending' -- so this pass, which already exists to
    # find state nobody is looking at, is the natural place to release it.
    try:
        await db_module.to_db_thread(
            _signal_state_repo.release_stranded_activations)
    except Exception as e:
        log.debug("[reconcile] stranded-claim sweep failed: %s", e)

    try:
        db_open = await db_module.to_db_thread(_reporting.get_open_trades)
        unknown = await db_module.to_db_thread(
            _signal_state_repo.fetch_unknown_signals)
    except Exception as e:
        log.debug("[reconcile] skipped — database read failed: %s", e)
        return None

    diff = diff_snapshots(positions, deals, db_open, unknown)
    report_periodic(diff)
    return diff
