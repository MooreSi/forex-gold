"""sync_channel_rename() (core_db_channel.py) -- cascades a Telegram
channel's real-title change across every table keyed by that name string.

Covers the 2026-07-24 fix: the function used to match on the RAW title
stored at select_group() time (e.g. "GOLD DIGGERS 2.0 ⚡️"), but every
trade/signal row is actually stored under the CANONICAL form ("Gold
Diggers 2.0") -- so a real rename matched zero existing rows and silently
created an orphaned second bucket instead of renaming the one already in
use, and the Channel Strategy tab kept showing the old name forever.

Also covers the SAME-DAY follow-up fix: the Channel Strategy tab's channel
list used to come from a hardcoded, in-memory CANONICAL_CHANNEL_ORDER array
that sync_channel_rename() patched in place -- but that patch never
survived an app restart (confirmed live via ticket 1650272215: a renamed
channel's trades kept recording correctly, but the tab reverted to showing
the stale pre-rename name as a dead ghost entry after every restart, while
the real channel never appeared at all). Fixed by dropping the in-memory
array entirely -- see _dynamic_channel_bucket_order()'s own docstring.
"""

import pytest

from backend.src.db import database as db
from backend.src.services.channels import repo as cdc


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def clean_canonical_names():
    """Seeds a synthetic variant/canonical pair for the mechanism tests below
    -- isolated from the real GD2.0/Institutional mapping (already fixed in
    CANONICAL_CHANNELS itself) so these tests don't depend on, or collide
    with, that specific live rename's current state."""
    names_snapshot = dict(cdc.CANONICAL_CHANNELS)
    cdc.CANONICAL_CHANNELS["RAW VARIANT TITLE ⚡️"] = "Old Canonical Name"
    cdc.CANONICAL_CHANNELS["999999999"] = "Old Canonical Name"
    cdc.CANONICAL_CHANNELS["Old Canonical Name"] = "Old Canonical Name"
    yield
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


def _insert_channel_parser_config(channel_name, created_at=0.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO channel_parser_config (channel_name, parser_format, signal_prefix, "
            "instant_entry_enabled, enabled, notes, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (channel_name, "gd2", "", 0, 1, "", created_at, created_at),
        )


def test_renaming_a_raw_variant_updates_the_canonical_bucket(clean_canonical_names):
    # "RAW VARIANT TITLE ⚡️" is a known raw variant of the canonical
    # "Old Canonical Name" bucket -- the rename must land on that bucket, not
    # create a disconnected new one keyed by the raw variant string.
    cdc.sync_channel_rename("RAW VARIANT TITLE ⚡️", "New Canonical Name")
    assert cdc._canonical("RAW VARIANT TITLE ⚡️") == "New Canonical Name"
    assert cdc._canonical("Old Canonical Name") == "New Canonical Name"
    assert cdc._canonical("999999999") == "New Canonical Name"


def test_renaming_updates_rows_stored_under_the_canonical_form(fresh_db, clean_canonical_names):
    # Real trade rows are stored under the canonical name ("Old Canonical
    # Name"), never the raw variant -- this is the regression the bug
    # caused: matching only on the raw variant updated nothing here.
    _insert_trade("t1", "Old Canonical Name")
    cdc.sync_channel_rename("RAW VARIANT TITLE ⚡️", "New Canonical Name")
    with db.db() as conn:
        tg_source = conn.execute(
            "SELECT tg_source FROM vantage_simulated_trades WHERE trade_id='t1'"
        ).fetchone()[0]
    assert tg_source == "New Canonical Name"


def test_renaming_also_updates_rows_stored_under_the_raw_variant(fresh_db, clean_canonical_names):
    # Belt-and-braces: any straggler row that happens to still hold the raw
    # (un-canonicalised) title must also be caught.
    _insert_trade("t2", "RAW VARIANT TITLE ⚡️")
    cdc.sync_channel_rename("RAW VARIANT TITLE ⚡️", "New Canonical Name")
    with db.db() as conn:
        tg_source = conn.execute(
            "SELECT tg_source FROM vantage_simulated_trades WHERE trade_id='t2'"
        ).fetchone()[0]
    assert tg_source == "New Canonical Name"


def test_rename_from_already_canonical_name_still_works(clean_canonical_names):
    # Backward-compat: internal callers that already pass the canonical
    # form directly (not a raw Telegram variant) must keep working.
    cdc.sync_channel_rename("Gold Diggers VIP", "Gold Diggers VIP Elite")
    assert cdc._canonical("Gold Diggers VIP") == "Gold Diggers VIP Elite"


def test_noop_when_names_equal(clean_canonical_names):
    before = dict(cdc.CANONICAL_CHANNELS)
    cdc.sync_channel_rename("Gold Diggers VIP", "Gold Diggers VIP")
    assert cdc.CANONICAL_CHANNELS == before


def test_noop_when_new_name_matches_existing_canonical_bucket(clean_canonical_names):
    # old_name resolves to "Old Canonical Name" already -- renaming "to"
    # that same canonical bucket is a no-op, not a self-referential rename.
    before = dict(cdc.CANONICAL_CHANNELS)
    cdc.sync_channel_rename("RAW VARIANT TITLE ⚡️", "Old Canonical Name")
    assert cdc.CANONICAL_CHANNELS == before


def test_blank_names_are_noop(clean_canonical_names):
    before = dict(cdc.CANONICAL_CHANNELS)
    cdc.sync_channel_rename("", "New Name")
    cdc.sync_channel_rename("Old Name", "")
    cdc.sync_channel_rename(None, None)
    assert cdc.CANONICAL_CHANNELS == before


# ── Dynamic channel listing survives a rename (and a restart) ──────────────

def test_renamed_channel_appears_dynamically_without_registration(fresh_db, clean_canonical_names):
    # No register_canonical_channel() call anywhere here (removed entirely,
    # 2026-07-24) -- the channel must show up purely because
    # channel_parser_config (which sync_channel_rename already cascades
    # correctly) now has its current name.
    _insert_channel_parser_config("RAW VARIANT TITLE ⚡️")
    cdc.sync_channel_rename("RAW VARIANT TITLE ⚡️", "New Canonical Name")
    settings = cdc.get_all_channel_strategy_settings()
    sources = [s["source"] for s in settings]
    assert "New Canonical Name" in sources
    assert "RAW VARIANT TITLE ⚡️" not in sources


def test_channel_list_derives_fresh_from_channel_parser_config_every_call(fresh_db):
    # No sync_channel_rename() call at all here, no prior in-memory state --
    # a channel_parser_config row alone (which is how a real Telegram slot
    # selection/restore populates it) must be enough for the channel to show
    # up, since the listing is derived from disk on every call rather than
    # from a module-level array that only a live rename event ever patched
    # and a process restart would silently reset.
    _insert_channel_parser_config("A Brand New Channel Never Registered")
    settings = cdc.get_all_channel_strategy_settings()
    sources = [s["source"] for s in settings]
    assert "A Brand New Channel Never Registered" in sources
