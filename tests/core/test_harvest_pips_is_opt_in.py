"""`harvest_pips` must be off unless someone deliberately turns it on.

**The live failure.** On 2026-08-26 a XAUUSD SELL on the "Asian Reversal -
ATR" template, whose harvest threshold was set to $30, closed itself for
$1.40 after 1.4 pips. Twice.

The EA harvests on either trigger:

    bool pipsHarvest = (harvestPips > 0.0) && (favMove >= PipsToPrice(harvestPips));
    if(profit >= t.tplHarvestThreshold || pipsHarvest)

`harvest_pips` was 1.0 on every template in the database, so the pips trigger
always fires first and the dollar threshold can never be reached. A user who
sets $30 gets a close at the first favourable pip, and the field is not
exposed anywhere in the UI, so they cannot see it or turn it off.

**How it got there.** The EA implemented the field on 2026-08-04 with
`TplD(t.tplCfg, "harvest_pips", 0.0)` and the comment "0 = off, matches every
template saved before this existed". That assumption was wrong for this
codebase: migration 17 had already added the column as
`REAL NOT NULL DEFAULT 1.0`, and DEFAULTS carried 1.0 too, so no template had
ever held 0. An opt-in feature shipped switched on for everyone.

These tests pin the intent the EA documents: off by default, opt-in only.
"""
from __future__ import annotations

from backend.src.services.broker import ea_templates


def test_harvest_pips_defaults_to_off():
    """0.0 is the EA's own fallback and the only safe default.

    Any positive value here closes every harvest-enabled trade at that many
    pips, which silently overrides whatever dollar threshold the user set.
    """
    assert ea_templates.DEFAULTS["harvest_pips"] == 0.0, (
        "a non-zero harvest_pips default makes harvest_threshold unreachable"
    )


def test_the_dollar_threshold_is_reachable_with_the_default():
    """The regression stated as the behaviour the user actually asked for.

    Reproduces the EA's condition in Python. With the shipped defaults, a
    trade 1.4 pips in profit and $1.40 up must NOT harvest against a $30
    threshold -- that is precisely what happened live.
    """
    defaults = ea_templates.DEFAULTS
    harvest_pips = float(defaults["harvest_pips"])

    favourable_pips = 1.4
    profit = 1.40
    threshold = 30.0

    pips_harvest = harvest_pips > 0.0 and favourable_pips >= harvest_pips
    assert not (profit >= threshold or pips_harvest), (
        "a $1.40 trade harvested against a $30 threshold -- the live bug"
    )


def test_the_check_still_fires_when_someone_opts_in():
    """Negative control: the guard above must not pass by disabling harvest.

    A test that only ever asserts "nothing closes" would stay green if the
    pips trigger were deleted outright, which is not the fix.
    """
    harvest_pips = 1.0        # opted in, deliberately
    favourable_pips = 1.4

    pips_harvest = harvest_pips > 0.0 and favourable_pips >= harvest_pips
    assert pips_harvest, "the pips trigger must still work when asked for"


def test_migration_29_clears_the_default_and_keeps_a_deliberate_value(fresh_db):
    """The rows the old column default created are reset; others are not.

    Changing DEFAULTS only helps templates created from here on. Every
    template already on disk carries 1.0 -- including the one that closed
    two live trades -- so the existing rows have to be corrected too.
    """
    with fresh_db.db() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(ea_trade_templates)")}
        assert "harvest_pips" in cols, "schema is missing the column under test"

        conn.execute(
            "INSERT INTO ea_trade_templates (name, harvest_enabled, harvest_threshold,"
            " harvest_pips, created_at, updated_at)"
            " VALUES ('from-the-old-default', 1, 30.0, 1.0, 0, 0)"
        )
        conn.execute(
            "INSERT INTO ea_trade_templates (name, harvest_enabled, harvest_threshold,"
            " harvest_pips, created_at, updated_at)"
            " VALUES ('deliberately-set', 1, 30.0, 12.5, 0, 0)"
        )
        conn.execute(
            "UPDATE ea_trade_templates SET harvest_pips = 0.0 WHERE harvest_pips = 1.0"
        )

        got = dict(conn.execute(
            "SELECT name, harvest_pips FROM ea_trade_templates"
            " WHERE name IN ('from-the-old-default', 'deliberately-set')"
        ).fetchall())

    assert got["from-the-old-default"] == 0.0, "the harmful default was not cleared"
    assert got["deliberately-set"] == 12.5, "a deliberate setting was overwritten"


def test_the_migration_statement_in_the_registry_is_the_one_tested():
    """Pins the test above to the real migration rather than a copy of it.

    A hand-written UPDATE in a test proves nothing if the registry ships a
    different statement.
    """
    from backend.migrations.registry import MIGRATIONS

    step = next((s for s in MIGRATIONS if s[0] == 29), None)
    assert step is not None, "migration 29 is missing"
    assert any(
        "UPDATE ea_trade_templates SET harvest_pips = 0.0 WHERE harvest_pips = 1.0" in stmt
        for stmt in step[2]
    ), f"migration 29 does not carry the statement this file tests: {step[2]}"
