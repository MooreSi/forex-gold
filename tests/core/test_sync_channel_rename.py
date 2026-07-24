"""sync_channel_rename() (core_db_channel.py) -- cascades a Telegram
channel's real-title change across every table keyed by that name string.

Covers the 2026-07-24 fix: the function used to match on the RAW title
stored at select_group() time (e.g. "GOLD DIGGERS 2.0 ⚡️"), but every
trade/signal row is actually stored under the CANONICAL form ("Gold
Diggers 2.0") -- so a real rename matched zero existing rows and silently
created an orphaned second bucket instead of renaming the one already in
use, and the Channel Strategy tab kept showing the old name forever.
"""
import os
import tempfile

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_db_channel as cdc


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


@pytest.fixture
def clean_canonical_state():
    order_snapshot = list(cdc.CANONICAL_CHANNEL_ORDER)
    names_snapshot = dict(cdc.CANONICAL_CHANNELS)
    yield
    cdc.CANONICAL_CHANNEL_ORDER[:] = order_snapshot
    cdc.CANONICAL_CHANNELS.clear()
    cdc.CANONICAL_CHANNELS.update(names_snapshot)


def _insert_trade(trade_id, tg_source):
    signal_id = f"sig-{trade_id}"
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (signal_id, "BUY", 2400.0, 2400.0, 2390.0, "active", 0.0),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, net_pnl, strategy, tg_source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, "BUY", 2400.0, 2400.0, 2400.0, 0.10, 0.0,
             2390.0, "closed", 0.0, 10.0, "scale_out", tg_source),
        )


def test_renaming_a_raw_variant_updates_the_canonical_bucket(clean_canonical_state):
    # "GOLD DIGGERS 2.0 ⚡️" is a known raw variant of the canonical
    # "Gold Diggers 2.0" bucket -- the rename must land on that bucket, not
    # create a disconnected new one keyed by the raw variant string.
    cdc.sync_channel_rename("GOLD DIGGERS 2.0 ⚡️", "GD Institutional Elite")
    assert "Gold Diggers 2.0" not in cdc.CANONICAL_CHANNEL_ORDER
    assert "GD Institutional Elite" in cdc.CANONICAL_CHANNEL_ORDER
    assert cdc._canonical("GOLD DIGGERS 2.0 ⚡️") == "GD Institutional Elite"
    assert cdc._canonical("Gold Diggers 2.0") == "GD Institutional Elite"
    assert cdc._canonical("2616846888") == "GD Institutional Elite"


def test_renaming_updates_rows_stored_under_the_canonical_form(fresh_db, clean_canonical_state):
    # Real trade rows are stored under the canonical name ("Gold Diggers
    # 2.0"), never the raw variant -- this is the regression the bug
    # caused: matching only on the raw variant updated nothing here.
    _insert_trade("t1", "Gold Diggers 2.0")
    cdc.sync_channel_rename("GOLD DIGGERS 2.0 ⚡️", "GD Institutional Elite")
    with db.db() as conn:
        tg_source = conn.execute(
            "SELECT tg_source FROM vantage_simulated_trades WHERE trade_id='t1'"
        ).fetchone()[0]
    assert tg_source == "GD Institutional Elite"


def test_renaming_also_updates_rows_stored_under_the_raw_variant(fresh_db, clean_canonical_state):
    # Belt-and-braces: any straggler row that happens to still hold the raw
    # (un-canonicalised) title must also be caught.
    _insert_trade("t2", "GOLD DIGGERS 2.0 ⚡️")
    cdc.sync_channel_rename("GOLD DIGGERS 2.0 ⚡️", "GD Institutional Elite")
    with db.db() as conn:
        tg_source = conn.execute(
            "SELECT tg_source FROM vantage_simulated_trades WHERE trade_id='t2'"
        ).fetchone()[0]
    assert tg_source == "GD Institutional Elite"


def test_rename_from_already_canonical_name_still_works(clean_canonical_state):
    # Backward-compat: internal callers that already pass the canonical
    # form directly (not a raw Telegram variant) must keep working.
    cdc.sync_channel_rename("Gold Diggers VIP", "Gold Diggers VIP Elite")
    assert "Gold Diggers VIP" not in cdc.CANONICAL_CHANNEL_ORDER
    assert "Gold Diggers VIP Elite" in cdc.CANONICAL_CHANNEL_ORDER


def test_noop_when_names_equal(clean_canonical_state):
    before = list(cdc.CANONICAL_CHANNEL_ORDER)
    cdc.sync_channel_rename("Gold Diggers VIP", "Gold Diggers VIP")
    assert cdc.CANONICAL_CHANNEL_ORDER == before


def test_noop_when_new_name_matches_existing_canonical_bucket(clean_canonical_state):
    # old_name resolves to "Gold Diggers 2.0" already -- renaming "to"
    # that same canonical bucket is a no-op, not a self-referential rename.
    before = list(cdc.CANONICAL_CHANNEL_ORDER)
    cdc.sync_channel_rename("GOLD DIGGERS 2.0 ⚡️", "Gold Diggers 2.0")
    assert cdc.CANONICAL_CHANNEL_ORDER == before


def test_blank_names_are_noop(clean_canonical_state):
    before = list(cdc.CANONICAL_CHANNEL_ORDER)
    cdc.sync_channel_rename("", "New Name")
    cdc.sync_channel_rename("Old Name", "")
    cdc.sync_channel_rename(None, None)
    assert cdc.CANONICAL_CHANNEL_ORDER == before
