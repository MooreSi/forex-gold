"""SELECT-only queries backing the trade-history views.

Every statement here was inline in `frontend/pages/history.py`, which is how a
NiceGUI page ended up owning multi-table joins against `vantage_ladder_legs`.
This is step 1 of draining that page: the queries move, the shaping stays where
it is, so the diff on the page is "expression moved" and nothing else.

Two things kept deliberately:

* **Rows out, not dicts.** These are the exact tuples the page already
  destructures. Converting them here would change the page's code in the same
  commit that moves the SQL, and then a behaviour change would be
  indistinguishable from a relocation. The dict conversion belongs in the
  controller step.
* **The `open_time >= cutoff` predicate.** Every history view is windowed, and
  dropping the bound would turn a bounded scan into a full-table one on a
  database that grows forever.

The ladder-leg pairs need explaining, because it is not obvious why nearly every
query appears twice. Legs 2+ of a laddered trade have a `vantage_ladder_legs`
row but no `vantage_simulated_trades` row of their own, so a query against
trades alone silently omits them. The `_for_legs` variants join back to the
parent trade to recover the attribution the leg never had.
"""
from __future__ import annotations

from typing import Any, Optional

import re as _re

from backend.src.db import database as db_module
from backend.src.utils.models import STRATEGY_NAMES
from backend.src.services.analytics.labels import (
    trade_channel_label, trade_source_label,
)


def _rows(sql: str, params: tuple = ()) -> list[tuple]:
    with db_module.db() as conn:
        return conn.execute(sql, params).fetchall()


# ── Channel attribution ───────────────────────────────────────────────────────

def ticket_sources(cutoff: float) -> list[tuple[Any, Any]]:
    """(mt5_ticket, tg_source) for trades opened since `cutoff`."""
    return _rows(
        "SELECT mt5_ticket, tg_source FROM vantage_simulated_trades "
        "WHERE mt5_ticket IS NOT NULL AND open_time >= ?",
        (cutoff,),
    )


def ticket_sources_for_legs(cutoff: float) -> list[tuple[Any, Any]]:
    """(mt5_ticket, tg_source) for ladder legs, inherited from the parent trade."""
    return _rows(
        "SELECT l.mt5_ticket, t.tg_source FROM vantage_ladder_legs l "
        "JOIN vantage_simulated_trades t ON t.trade_id = l.trade_id "
        "WHERE l.mt5_ticket IS NOT NULL AND t.open_time >= ?",
        (cutoff,),
    )


# ── Strategy attribution ──────────────────────────────────────────────────────

def ticket_strategies(cutoff: float) -> list[tuple[Any, Any, Any]]:
    """(mt5_ticket, strategy, dpm_trade_id) since `cutoff`.

    LEFT JOIN, not INNER: a trade with no dpm_trade_performance row must still
    appear, carrying its own strategy. An inner join would silently drop every
    non-DPM trade from the history view.
    """
    return _rows(
        "SELECT t.mt5_ticket, t.strategy, d.trade_id "
        "FROM vantage_simulated_trades t "
        "LEFT JOIN dpm_trade_performance d ON t.trade_id = d.trade_id "
        "WHERE t.mt5_ticket IS NOT NULL AND t.open_time >= ?",
        (cutoff,),
    )


def ticket_strategies_for_legs(cutoff: float) -> list[tuple[Any, Any]]:
    """(mt5_ticket, strategy) for ladder legs, inherited from the parent."""
    return _rows(
        "SELECT l.mt5_ticket, t.strategy FROM vantage_ladder_legs l "
        "JOIN vantage_simulated_trades t ON t.trade_id = l.trade_id "
        "WHERE l.mt5_ticket IS NOT NULL AND t.open_time >= ?",
        (cutoff,),
    )


# ── Order type ────────────────────────────────────────────────────────────────

def ticket_order_types(cutoff: float) -> list[tuple[Any, Any, Optional[float]]]:
    """(mt5_ticket, order_type, pending_placed_at) since `cutoff`."""
    return _rows(
        "SELECT mt5_ticket, order_type, pending_placed_at FROM vantage_simulated_trades "
        "WHERE mt5_ticket IS NOT NULL AND open_time >= ?",
        (cutoff,),
    )


# ── Ladder grouping ───────────────────────────────────────────────────────────

def ticket_groups() -> list[tuple[Any, Any, Any]]:
    """(trade_id, ticket, tier) across parent trades and their ladder legs.

    UNION ALL rather than UNION: the two halves are disjoint by construction --
    parents come from vantage_simulated_trades, legs from vantage_ladder_legs --
    so deduplicating would only cost a sort over the whole result.

    The parent half is tier 1 by definition; only trades that actually have legs
    are included, via the IN subquery.
    """
    return _rows(
        "SELECT t.trade_id, t.mt5_ticket AS ticket, 1 AS tier "
        "FROM vantage_simulated_trades t "
        "WHERE t.trade_id IN (SELECT DISTINCT trade_id FROM vantage_ladder_legs) "
        "AND t.mt5_ticket IS NOT NULL "
        "UNION ALL "
        "SELECT l.trade_id, l.mt5_ticket AS ticket, l.tier "
        "FROM vantage_ladder_legs l WHERE l.mt5_ticket IS NOT NULL"
    )


# ── Full ticket info (unwindowed) ─────────────────────────────────────────────
#
# These two have no cutoff on purpose: they back the consolidated ledger view,
# which reconciles against tickets of any age. Adding a window here would make
# older consolidated rows lose their channel and strategy labels.

def all_ticket_info() -> list[tuple[Any, Any, Any, Any]]:
    """(mt5_ticket, tg_source, strategy, direction) for every ticketed trade."""
    return _rows(
        "SELECT mt5_ticket, tg_source, strategy, direction "
        "FROM vantage_simulated_trades WHERE mt5_ticket IS NOT NULL"
    )


def all_ticket_info_for_legs() -> list[tuple[Any, Any, Any, Any]]:
    """(mt5_ticket, tg_source, strategy, direction) for every ticketed leg."""
    return _rows(
        "SELECT l.mt5_ticket, t.tg_source, t.strategy, t.direction "
        "FROM vantage_ladder_legs l "
        "JOIN vantage_simulated_trades t ON t.trade_id = l.trade_id "
        "WHERE l.mt5_ticket IS NOT NULL"
    )


# ── Moved out of frontend/pages/history.py by the 2026-08-25 merge ────────────
# Both arrived with upstream, doing raw SQL from inside a NiceGUI page. The
# frontend-never-imports-the-database contract is enforced at zero
# (tests/refactor/test_import_contracts.py), so the queries move down here and
# the shaping stays on the page -- the same "expression moved, nothing else"
# split this module's docstring describes.

_STRAT_LABEL = {
    "scale_out":       "Scale Out",
    "be_runner":       "BE Runner",
    "trail_stop":      "Trail Stop",
    "protected_scale": "Protected Scale",
    "conservative":    "Conservative",
}

_COPIER_COMMENT_RE = _re.compile(r"^C(\d+)_[A-Z0-9]+_\d+_(?:ANC|PEN)$", _re.IGNORECASE)


def _strategy_display_label(strategy: str) -> str:
    """Human-readable label for a trade's strategy, including EA Templates
    ("template:<name>") -- these are user-defined, not one of the fixed
    built-in strategies, so they were never in STRATEGY_NAMES/_STRAT_LABEL
    and fell through to the "—" placeholder instead of a readable name.
    Confirmed live 2026-07-23 that every EA Template trade showed a blank
    Strategy column in Trade Analysis."""
    if not strategy:
        return "—"
    from backend.src.services.broker import ea_templates as _et
    if _et.is_template_override(strategy):
        return f"Template: {_et.template_name_from_override(strategy)}"
    return _STRAT_LABEL.get(strategy, STRATEGY_NAMES.get(strategy, "—"))


def _template_group_map(leg_comments: dict) -> dict[str, tuple[str, int]]:
    """Return {ticket_str: (trade_id, tier)} for every ticket belonging to an
    EA Template group of 2+ legs, in the exact shape _ticket_group_map
    already produces for Adaptive Runner ladder legs -- merged into the same
    group_map in _render_trade_table so the row-collapsing logic that
    already exists for ladder legs applies to template siblings too, with no
    changes needed to that logic itself.

    Before this, a grid trade's anchor and its 2-3 sibling legs each
    rendered as their own unrelated row -- _template_leg_maps only
    backfilled their blank Channel/Strategy columns, nothing summed what the
    signal actually made. tier 1 is always the leg that promoted the local
    trade row (matches _ticket_group_map's own "tier 1 = anchor"
    convention); the rest sort by ticket number, which the broker issues in
    fill order. A prefix with only one resolved ticket is left out entirely
    -- a group of one is nothing to collapse, same rule _ticket_group_map's
    own caller applies.

    Module-level (unlike _template_leg_maps) since it has no dependency on
    _render_trade_table's own closure -- only leg_comments and the DB.
    """
    from backend.src.services.broker.ea_bridge import trade_id_prefix_from_comment

    by_prefix: dict[str, list[str]] = {}
    for ticket, comment in (leg_comments or {}).items():
        prefix = trade_id_prefix_from_comment(comment)
        if prefix:
            by_prefix.setdefault(prefix, []).append(str(ticket))

    result: dict[str, tuple[str, int]] = {}
    try:
        with db_module.db() as conn:
            for prefix, tickets in by_prefix.items():
                if len(tickets) < 2:
                    continue
                row = conn.execute(
                    "SELECT trade_id, mt5_ticket FROM vantage_simulated_trades "
                    "WHERE trade_id LIKE ? LIMIT 1",
                    (prefix + "%",),
                ).fetchone()
                if not row:
                    continue
                trade_id = row[0]
                anchor_ticket = str(row[1]) if row[1] else None
                ordered = sorted(tickets, key=lambda t: (t != anchor_ticket, int(t)))
                for tier, ticket in enumerate(ordered, start=1):
                    result[ticket] = (trade_id, tier)
    except Exception:
        pass
    return result


def _comment_attribution_maps(leg_comments: dict) -> tuple[dict, dict, dict]:
    """Return ({ticket: channel}, {ticket: strategy}, {ticket: max_tp_hit})
    recovered from the broker's own opening-deal comment, for broker positions
    that have no vantage_simulated_trades row of their own.

    `leg_comments` is {ticket: entry_deal_comment}, taken from the broker's
    deal history by the caller. Three comment shapes are recognised, all of
    them written by something that leaves no local row behind:

    "ea:<trade_id[:10]><a|g><N>" -- an EA Template leg. A template trade opens
        one broker position per Anchor/Grid leg, but Python keeps a SINGLE
        vantage_simulated_trades row per trade, so every leg except the one
        that promoted that row has no local row and no ticket lookup can find
        it. The EA's comment is the link back (see ea_bridge.
        trade_id_prefix_from_comment) -- the same mechanism
        core_template_placeholder_repair uses to adopt an orphaned row. Over
        two days of live history only 59 of 294 broker positions had a local
        row, and 160 of the remaining 235 were template legs.

    "sig:<signal_id[:8]>" -- this app's own non-template order comment (see
        core_open_trade.py). A position carrying it IS ours; reaching here
        means the trade row lost its mt5_ticket link, so recover the channel
        through signal_id instead.

    "C<n>_..._ANC|_PEN" -- the third-party copier EA. See
        _COPIER_COMMENT_RE above.

    Module-level so both the Closed Trades table and the calendar's
    day-detail view attribute a ticket identically -- the calendar had no
    comment-based fallback at all, so every template leg (and every copier
    position) showed "Unknown" there while the table beside it resolved the
    same ticket correctly.

    Max TP Hit (2026-08-07) travels the same route for the same reason: it is
    only ever computed against a vantage_simulated_trades row, so a leg with
    no row of its own showed a permanent "..." ("updating in 30 min") that no
    sweep was ever going to replace. Measured on this account: of 2498 broker
    positions the Closed Trades table has rendered, only 585 could resolve a
    Max TP -- 77% of the table stuck on "...". Every leg of a template trade
    belongs to ONE signal and is measured against that signal's TP ladder, so
    the parent row's value is the answer for the whole trade, legs included.
    Copier-EA positions are not ours and have no ladder to measure against at
    all, so they get the "n/a" sentinel -- rendered as a plain dash rather
    than a promise of an update that will never come.
    """
    from backend.src.services.broker.ea_bridge import trade_id_prefix_from_comment

    src: dict[str, str] = {}
    strat: dict[str, str] = {}
    max_tp: dict[str, str] = {}

    by_prefix: dict[str, list] = {}
    by_signal: dict[str, list] = {}
    for ticket, comment in (leg_comments or {}).items():
        comment = comment or ""
        prefix = trade_id_prefix_from_comment(comment)
        if prefix:
            by_prefix.setdefault(prefix, []).append(str(ticket))
            continue
        if comment.startswith("sig:"):
            sig_prefix = comment[len("sig:"):].strip()
            if sig_prefix:
                by_signal.setdefault(sig_prefix, []).append(str(ticket))
            continue
        m = _COPIER_COMMENT_RE.match(comment)
        if m:
            src[str(ticket)] = f"Copier EA (C{int(m.group(1))})"
            strat[str(ticket)] = "External"
            max_tp[str(ticket)] = "n/a"

    if not by_prefix and not by_signal:
        return src, strat, max_tp

    # Prefer a row that actually has max_tp_hit: a template trade can leave
    # more than one row sharing a trade_id/signal_id prefix, and picking an
    # arbitrary one would blank the column for legs whose sibling row was
    # already computed.
    _SQL_BY_TRADE_ID = ("SELECT tg_source, strategy, max_tp_hit "
                        "FROM vantage_simulated_trades WHERE trade_id LIKE ? "
                        "ORDER BY max_tp_hit IS NULL LIMIT 1")
    _SQL_BY_SIGNAL_ID = ("SELECT tg_source, strategy, max_tp_hit "
                         "FROM vantage_simulated_trades WHERE signal_id LIKE ? "
                         "ORDER BY max_tp_hit IS NULL LIMIT 1")
    try:
        with db_module.db() as conn:
            for sql, groups in ((_SQL_BY_TRADE_ID, by_prefix),
                                (_SQL_BY_SIGNAL_ID, by_signal)):
                for prefix, tickets in groups.items():
                    row = conn.execute(sql, (prefix + "%",)).fetchone()
                    if not row:
                        continue
                    tg_source, strategy, parent_max_tp = row[0], row[1], row[2]
                    ch = trade_channel_label(tg_source or "")
                    label = ch if ch else trade_source_label(tg_source or "")
                    for ticket in tickets:
                        src[ticket] = label
                        strat[ticket] = _strategy_display_label(strategy or "")
                        # Left unset when the parent hasn't been computed yet,
                        # so the leg keeps showing "..." and picks the real
                        # value up on a later refresh.
                        if parent_max_tp:
                            max_tp[ticket] = parent_max_tp
    except Exception:
        pass
    return src, strat, max_tp

