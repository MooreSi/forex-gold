"""The EA template's own description of its fields.

The owner, 2026-09-19: "on trading > ea templates i'm unable to edit a
template". They could not. The editor was a ten-row textarea holding the
template as raw JSON -- **around a hundred keys**, unlabelled, ungrouped,
with no indication of which are booleans, which are enums with a fixed set of
values, or what any of them mean. Saving invalid JSON was caught; saving
`trail_mode: "stepp"` was not, and reached the service as a ValueError.

The fix is a real form, and a real form needs to know what the fields ARE.
That knowledge already exists here -- `DEFAULTS`, `_BOOL_FIELDS`,
`_INT_FIELDS`, `_FLOAT_FIELDS` and `_CHOICES` between them describe every
field completely. This exposes it rather than re-typing it in TypeScript,
because a hand-maintained copy of a hundred field types in another language
drifts the first time somebody adds a field here and nowhere else.

The property that matters is total coverage: every key the service will
accept must be described, or the form has a field the operator cannot reach.
"""
from __future__ import annotations

import pytest

from backend.src.services.broker import ea_templates as tpl


@pytest.fixture
def schema():
    return {f["name"]: f for f in tpl.field_schema()}


class TestItDescribesEverything:

    def test_every_saveable_field_is_described(self, schema):
        # The one that stops a field becoming unreachable: `_clean_fields`
        # accepts exactly the DEFAULTS keys, so anything missing here is a
        # setting the form cannot show.
        assert set(schema) == set(tpl.DEFAULTS)

    def test_it_describes_nothing_the_service_would_drop(self, schema):
        # The mirror image. A field in the form that `_clean_fields` discards
        # is a control that appears to work and does nothing.
        assert set(schema) <= set(tpl.DEFAULTS)

    def test_bookkeeping_is_not_offered_as_a_setting(self, schema):
        for field in ("name", "created_at", "updated_at"):
            assert field not in schema


class TestTheTypes:

    def test_a_switch_is_a_boolean(self, schema):
        assert schema["tg_cmd_enabled"]["type"] == "boolean"

    def test_a_count_is_an_integer(self, schema):
        assert schema["anchors"]["type"] == "integer"

    def test_a_distance_is_a_number(self, schema):
        assert schema["sl_pips"]["type"] == "number"

    def test_a_fixed_set_is_a_choice(self, schema):
        assert schema["trail_mode"]["type"] == "choice"

    def test_every_field_has_exactly_one_type(self, schema):
        allowed = {"boolean", "integer", "number", "choice", "text"}
        assert {f["type"] for f in schema.values()} <= allowed

    def test_a_choice_carries_the_values_it_will_accept(self, schema):
        # Without them the form is a free-text box over a validated enum,
        # which is the textarea again with extra steps.
        assert set(schema["trail_mode"]["choices"]) == set(tpl.TRAIL_MODE_CHOICES)

    def test_a_non_choice_offers_no_values(self, schema):
        assert schema["sl_pips"]["choices"] == []

    def test_every_choice_field_in_the_service_is_marked_as_one(self, schema):
        for name in ("mode", "tpsl_mode", "anchor", "trail_mode", "be_mode",
                     "pending_mode"):
            assert schema[name]["type"] == "choice", name


class TestTheDefaults:

    def test_each_field_carries_the_value_a_new_template_gets(self, schema):
        for name, field in schema.items():
            assert field["default"] == tpl.DEFAULTS[name], name

    def test_a_default_is_of_the_type_the_field_claims(self, schema):
        for field in schema.values():
            if field["type"] == "boolean":
                assert isinstance(field["default"], bool), field["name"]
            elif field["type"] == "integer":
                assert isinstance(field["default"], int), field["name"]
            elif field["type"] == "number":
                assert isinstance(field["default"], (int, float)), field["name"]

    def test_a_choice_defaults_to_one_of_its_own_choices(self, schema):
        # A default outside its own enum makes every new template invalid.
        for field in schema.values():
            if field["type"] == "choice":
                assert field["default"] in field["choices"], field["name"]


class TestItIsStable:

    def test_the_order_does_not_wander_between_calls(self):
        # The form renders in this order. A set-derived order would reshuffle
        # the page on every reload.
        assert [f["name"] for f in tpl.field_schema()] == \
               [f["name"] for f in tpl.field_schema()]

    def test_it_reads_the_schema_rather_than_a_saved_row(self):
        # No database, no template, no account. This is a description of the
        # shape, and it must answer on a fresh install with nothing saved.
        assert len(tpl.field_schema()) > 50
