"""What each training dataset looks like to the merge: how a node exports its
own rows, what time orders them, and when two rows are the same event.

Between 2026-09-29 and 10-13 both nodes generated signals, so one market
move can sit in both histories. Counted twice, it would be weighted twice.
A row from the peer is dropped when this node already holds the same event:

  re   same direction, created within 300 s, level within 1.0 point
  bo   same direction and breakout type, created within 300 s, entry within 1.0
  pro  a positive with the same Telegram message id (every reader of a
       channel sees the same id); a background sample in the same 15-minute
       slot and direction (each node samples every 15 minutes on its own
       clock)
"""
from __future__ import annotations

from typing import Callable, Optional

_WINDOW_S = 300.0
_PRICE_TOL = 1.0
_BG_SLOT_S = 900.0


def _f(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# What orders a kind's training rows. Breakout's reader sorts by close time,
# the other two by creation/capture time.
ORDER: dict[str, Callable[[dict], float]] = {
    "re": lambda r: _f(r.get("created_at")) or 0.0,
    "bo": lambda r: _f(r.get("close_time")) or _f(r.get("created_at")) or 0.0,
    "pro": lambda r: _f(r.get("captured_at")) or 0.0,
}


def export_page(kind: str, after: int, limit: int) -> list[dict]:
    """This node's own labelled rows of `kind` with id > after, by id."""
    if kind == "re":
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        return re_db.export_ml_rows(after, limit)
    if kind == "bo":
        from backend.src.services.breakout_signal import breakout_signal_repo as bdb
        return bdb.export_ml_rows(after, limit)
    if kind == "pro":
        from backend.src.services.reversal_engine import pro_corpus_repo as pro_corpus
        return pro_corpus.export_rows(after, limit)
    raise ValueError(f"unknown training dataset {kind!r}")


def _event_key(kind: str, r: dict) -> tuple:
    if kind == "bo":
        return (r.get("direction"), r.get("breakout_type"))
    return (r.get("direction"),)


def _price(kind: str, r: dict) -> Optional[float]:
    return _f(r.get("entry_mid" if kind == "bo" else "level_price"))


def _signal_duplicates(kind: str, own: list[dict], peer: list[dict]) -> list[dict]:
    buckets: dict[tuple, list[dict]] = {}
    for r in own:
        t = _f(r.get("created_at"))
        if t is not None:
            buckets.setdefault(_event_key(kind, r) + (int(t // _WINDOW_S),), []).append(r)
    kept = []
    for r in peer:
        t = _f(r.get("created_at"))
        if t is None:
            kept.append(r)
            continue
        key = _event_key(kind, r)
        b = int(t // _WINDOW_S)
        near = (o for k in (b - 1, b, b + 1) for o in buckets.get(key + (k,), ()))
        p = _price(kind, r)
        if not any(abs((_f(o.get("created_at")) or 0.0) - t) <= _WINDOW_S
                   and (p is None or _price(kind, o) is None
                        or abs(_price(kind, o) - p) <= _PRICE_TOL)
                   for o in near):
            kept.append(r)
    return kept


def _pro_duplicates(own: list[dict], peer: list[dict]) -> list[dict]:
    posts = {r.get("tg_message_id") for r in own
             if not str(r.get("tg_message_id") or "").startswith("bg-")}
    slots = {(r.get("direction"), int((_f(r.get("captured_at")) or 0.0) // _BG_SLOT_S))
             for r in own if str(r.get("tg_message_id") or "").startswith("bg-")}
    kept = []
    for r in peer:
        if r.get("stage") == "background":
            slot = (r.get("direction"), int((_f(r.get("captured_at")) or 0.0) // _BG_SLOT_S))
            if slot not in slots:
                kept.append(r)
        elif r.get("tg_message_id") not in posts:
            kept.append(r)
    return kept


def drop_duplicates(kind: str, own: list[dict], peer: list[dict]) -> list[dict]:
    """The peer rows that are not an event this node already holds."""
    if kind == "pro":
        return _pro_duplicates(own, peer)
    return _signal_duplicates(kind, own, peer)
