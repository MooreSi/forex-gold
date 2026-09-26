"""A queued channel-strategy change under the pre-rebrand name is dropped.

Found 2026-09-26: the Mac's resend queue (sync_pending_channel_strategy) held
{'GD Copy Engine': {'strategy': 'conservative', 'auto': False}} from before
the 2026-07-23 rebrand. It was re-sent on every reconnect, and the VPS
applied it, which kept creating the duplicate row that crash-looped the VPS's
startup (backfills._rebrand_source_names, now OR IGNORE).

Dropped, not renamed: renaming would push that stale "conservative" over
Reversal Engine's real strategy on the VPS (on the Mac it was
'template:30 TP1 SL50 and Trail'). Nothing here reaches a socket or a broker.
"""
from __future__ import annotations

import json

from backend.src.services.cluster.sync import _pending_store as ps


def _stored(monkeypatch, value):
    store = {"sync_pending_channel_strategy": json.dumps(value)}
    monkeypatch.setattr(ps.db_module, "get_app_config", lambda k: store.get(k))
    monkeypatch.setattr(ps.db_module, "set_app_config", lambda k, v: store.__setitem__(k, v))
    return store


def test_the_old_name_is_dropped_and_the_rest_kept(monkeypatch):
    _stored(monkeypatch, {"GD Copy Engine": {"strategy": "conservative", "auto": False},
                          "Gold Diggers 2.0": {"strategy": "template:Test", "auto": False}})

    loaded = ps.PendingStoreMixin._load_pending_channel_strategy()

    assert loaded == {"Gold Diggers 2.0": {"strategy": "template:Test", "auto": False}}


def test_it_is_not_renamed_onto_reversal_engine(monkeypatch):
    _stored(monkeypatch, {"GD Copy Engine": {"strategy": "conservative", "auto": False}})

    assert "Reversal Engine" not in ps.PendingStoreMixin._load_pending_channel_strategy()


def test_the_cleaned_queue_is_saved_so_it_stays_gone(monkeypatch):
    store = _stored(monkeypatch, {"GD Copy Engine": {"strategy": "conservative"}})

    ps.PendingStoreMixin._load_pending_channel_strategy()

    assert "GD Copy Engine" not in json.loads(store["sync_pending_channel_strategy"])


def test_a_queue_without_it_is_left_as_it_is(monkeypatch):
    writes = []
    _stored(monkeypatch, {"Gold Diggers 2.0": {"strategy": "x"}})
    monkeypatch.setattr(ps.db_module, "set_app_config", lambda k, v: writes.append(k))

    assert ps.PendingStoreMixin._load_pending_channel_strategy() == {"Gold Diggers 2.0": {"strategy": "x"}}
    assert writes == []
