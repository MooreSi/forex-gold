"""Renaming an EA template (Trading > Strategy > EA Templates).

**A template's name is a foreign key nobody declared.** A strategy override is
stored as the string `template:<name>` in the trading schedule, on a channel,
in the AI's recommendation and in the global risk settings -- and
`template_for_channel` resolves those routes IN ORDER, falling through to the
next one when a name does not resolve. So a rename that left the references
behind would not raise and would not show an error: the channel would quietly
begin trading under a DIFFERENT strategy, which is the failure this whole file
exists to prevent.

The other half is the part that must NOT move. A closed trade recorded as
"Template: Old Name" was placed under that name, and rewriting history to say
otherwise would falsify the record the P&L attribution is built from.
"""
import json

import pytest

from backend.src.services.broker import ea_templates as et
from backend.src.services.risk import schedule as sched
from backend.src.services.channels import repo as ch_repo
from backend.src.db import database as db


def _make(name: str, **over):
    et.save_ea_template(name, {"sl_pips": 50.0, "tp1_pips": 30.0, **over})


class TestTheRenameItself:
    def test_the_template_answers_to_its_new_name(self, fresh_db):
        _make("Old Name", sl_pips=65.0)

        et.rename_ea_template("Old Name", "New Name")

        renamed = et.get_ea_template("New Name")
        assert renamed is not None
        assert renamed["sl_pips"] == 65.0

    def test_the_old_name_stops_resolving(self, fresh_db):
        _make("Old Name")

        et.rename_ea_template("Old Name", "New Name")

        assert et.get_ea_template("Old Name") is None

    def test_every_field_survives_the_move(self, fresh_db):
        """A rename that quietly reset a field to its default would hand the
        EA a different strategy under the name the operator trusted."""
        _make("Old Name", mode="grid", grid_step_pts=12.5, trail_mode="fractal",
              be_buffer_pts=7.0, partials=False, tp3_pct=15.0)
        before = et.get_ea_template("Old Name")

        et.rename_ea_template("Old Name", "New Name")

        after = et.get_ea_template("New Name")
        skip = {"name", "updated_at"}
        assert {k: v for k, v in after.items() if k not in skip} == \
               {k: v for k, v in before.items() if k not in skip}

    def test_the_original_creation_date_travels_with_it(self, fresh_db):
        _make("Old Name")
        created = et.get_ea_template("Old Name")["created_at"]

        et.rename_ea_template("Old Name", "New Name")

        assert et.get_ea_template("New Name")["created_at"] == created


class TestWhatItRefuses:
    def test_renaming_something_that_is_not_there(self, fresh_db):
        with pytest.raises(ValueError, match="No EA template"):
            et.rename_ea_template("Ghost", "New Name")

    def test_renaming_onto_an_existing_template(self, fresh_db):
        """Silently overwriting would destroy a tuned template that the
        operator can no longer get back."""
        _make("Old Name")
        _make("Taken", sl_pips=99.0)

        with pytest.raises(ValueError, match="already"):
            et.rename_ea_template("Old Name", "Taken")

        assert et.get_ea_template("Taken")["sl_pips"] == 99.0
        assert et.get_ea_template("Old Name") is not None

    def test_a_blank_new_name(self, fresh_db):
        _make("Old Name")

        with pytest.raises(ValueError, match="name is required"):
            et.rename_ea_template("Old Name", "   ")

        assert et.get_ea_template("Old Name") is not None

    def test_renaming_to_the_same_name_changes_nothing(self, fresh_db):
        """Not an error -- the operator asked for a state that already holds.
        It must not delete the template on its way to achieving it."""
        _make("Old Name", sl_pips=65.0)

        et.rename_ea_template("Old Name", "Old Name")

        assert et.get_ea_template("Old Name")["sl_pips"] == 65.0

    def test_surrounding_whitespace_is_not_a_new_name(self, fresh_db):
        _make("Old Name")

        et.rename_ea_template("Old Name", "  Tidier Name  ")

        assert et.get_ea_template("Tidier Name") is not None


class TestTheReferencesThatMustFollow:
    def test_a_schedule_window_keeps_pointing_at_the_same_template(self, fresh_db):
        _make("Old Name")
        schedule = sched.get_trading_schedule()
        schedule["monday"][0]["reversal_engine_override"] = "template:Old Name"
        schedule["monday"][0]["strategy_override"] = "template:Old Name"
        sched.set_trading_schedule(schedule)

        et.rename_ea_template("Old Name", "New Name")

        after = sched.get_trading_schedule()["monday"][0]
        assert after["reversal_engine_override"] == "template:New Name"
        assert after["strategy_override"] == "template:New Name"

    def test_a_channel_window_inside_the_schedule_follows_too(self, fresh_db):
        """Per-channel overrides are nested a level deeper in the same JSON.
        A sweep that only walked the top-level keys would miss them."""
        _make("Old Name")
        schedule = sched.get_trading_schedule()
        schedule["monday"][0]["telegram_channels"] = {
            "Gold Diggers VIP": {"enabled": True,
                                 "strategy_override": "template:Old Name"},
        }
        sched.set_trading_schedule(schedule)

        et.rename_ea_template("Old Name", "New Name")

        after = sched.get_trading_schedule()["monday"][0]["telegram_channels"]
        assert after["Gold Diggers VIP"]["strategy_override"] == "template:New Name"

    def test_a_channel_assignment_follows(self, fresh_db):
        _make("Old Name")
        ch_repo.set_channel_strategy_override("Gold Diggers VIP",
                                              "template:Old Name", False)

        et.rename_ea_template("Old Name", "New Name")

        assert ch_repo.get_channel_strategy_override("Gold Diggers VIP") == \
            "template:New Name"

    def test_the_ai_recommendation_follows(self, fresh_db):
        """`template_for_channel` reads the recommendation when there is no
        explicit assignment. A stale one there does not error -- it falls
        through to the global strategy, which is a different trade."""
        _make("Old Name")
        ch_repo.set_channel_strategy_rec("Reversal Engine", "template:Old Name",
                                         "because", 0.7)

        et.rename_ea_template("Old Name", "New Name")

        assert ch_repo.get_channel_strategy_rec("Reversal Engine")["strategy"] \
            == "template:New Name"

    def test_the_global_strategy_follows(self, fresh_db):
        _make("Old Name")
        db.update_risk_settings({"trade_strategy": "template:Old Name",
                                 "display_strategy_id": "template:Old Name"})

        et.rename_ea_template("Old Name", "New Name")

        rs = db.get_risk_settings()
        assert rs["trade_strategy"] == "template:New Name"
        assert rs["display_strategy_id"] == "template:New Name"

    def test_a_reference_to_a_different_template_is_left_alone(self, fresh_db):
        """The sweep matches the whole override, not a substring. "Old Name"
        and "Old Name v2" are two templates."""
        _make("Old Name")
        _make("Old Name v2")
        db.update_risk_settings({"trade_strategy": "template:Old Name v2"})

        et.rename_ea_template("Old Name", "New Name")

        assert db.get_risk_settings()["trade_strategy"] == "template:Old Name v2"

    def test_it_reports_what_it_repointed(self, fresh_db):
        """The operator has to be able to see that a rename moved live
        trading configuration, not just a label."""
        _make("Old Name")
        db.update_risk_settings({"trade_strategy": "template:Old Name"})

        result = et.rename_ea_template("Old Name", "New Name")

        assert result["repointed"]["risk_settings"] == 1


class TestWhatMustNotMove:
    def test_a_closed_trade_keeps_the_name_it_was_placed_under(self, fresh_db):
        """History is a record of what happened. A trade placed under "Old
        Name" was placed under "Old Name", and the P&L attribution behind the
        Analysis tab is built from exactly this string."""
        _make("Old Name")
        with db.db() as conn:
            conn.execute(
                "INSERT INTO vantage_signals "
                "(signal_id, source_name, direction, entry_low, entry_high, "
                " stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
                ("sig-1", "Gold Diggers VIP", "BUY", 2400.0, 2400.0, 2390.0,
                 "closed", 1_750_000_000.0))
            conn.execute(
                "INSERT INTO vantage_simulated_trades "
                "(signal_id, direction, entry_low, entry_high, stop_loss, "
                " entry_price, lot_size, remaining_lots, open_time, strategy) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("sig-1", "BUY", 2400.0, 2400.0, 2390.0, 2400.0, 0.01, 0.0,
                 1_750_000_000.0, "template:Old Name"))

        et.rename_ea_template("Old Name", "New Name")

        with db.db() as conn:
            kept = conn.execute(
                "SELECT strategy FROM vantage_simulated_trades "
                "WHERE signal_id='sig-1'").fetchone()[0]
        assert kept == "template:Old Name"
