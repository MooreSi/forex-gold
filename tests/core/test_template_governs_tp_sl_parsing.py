"""When an EA template governs a channel, the template decides its TP and SL.

Reported live 2026-09-09. GOLD DIGGERS INSTITUTIONAL posted:

    BUY GOLD @ 4394/4388
    TP 4396 / TP 4399 / TP 4403 / TP OPEN
    SL 4387

and the app answered:

    Entry: 4388.0 - 4394.0
    SL: 4383.0
    Limit order skipped - signal has no TP levels.

**The parser was not at fault.** `parse_limit_order_signal` returns SL 4387.0,
tp1/2/3 = 4396/4399/4403 and `tp_open` True from that exact text -- verified
directly. Two things happened after it:

1. `lk_enable_tp_parsing` is OFF, so `scan_messages` stripped every TP to None,
   and `limit_order_signal` then refused the order for having none.
2. `lk_enable_sl_parsing` is OFF, so `apply_sl_parsing_override` replaced 4387
   with 4383. **That part was correct** and is not a bug: the governing
   template is `30 TP1 SL50 and Trail`, whose `sl_pips` is 50, and 50 pips
   from the zone edge IS 4383. An earlier reading of this as a wrong fallback
   came from querying the pre-migration database, where the channel override
   was absent -- recorded because the mistake is easy to repeat.

Both toggles are global. The channel's governing template is
`30 TP1 SL50 and Trail` (`sl_pips` 50, `tp1_pips` 40, `tp2_pips` 50,
`tp_from_telegram` 0), so with this fix it supplies its own two targets and the
order is placed instead of refused. A template with `tp_from_telegram` set --
`GD Instituational - single`, which 40 of this channel's earlier trades ran
under -- keeps the signal's own targets instead. Both paths are covered below.

**The principle, already stated in `apply_sl_parsing_override`'s own docstring:**
"a template is a self-contained per-channel definition and already outranks the
signal's own stop in core_signal_resolution -- so it must win here too". A
template is more specific than a global toggle, so the template decides.

**The lookup is still narrowed** even though the live case did not need it:
`apply_sl_parsing_override` resolved the template with
`get_channel_strategy_override` alone, so a channel governed by the AI
recommendation or the global strategy silently got generic distances instead of
its template's. One signal, several resolution routes, only one asked -- the
same shape as bugs/024 and reversal-engine/080 -- so both overrides now share
one resolver whose order matches the execution path's.
"""
from __future__ import annotations

import pytest

from backend.src.services.broker import ea_templates


TEMPLATE = {
    "name": "GD Instituational - single",
    "sl_pips": 70.0,
    "tp1_pips": 30.0, "tp2_pips": 70.0, "tp3_pips": 120.0,
    "tp_from_telegram": 1,
}
CHANNEL = "GOLD DIGGERS INSTITUTIONAL"


def _signal():
    return {"direction": "BUY", "entry_low": 4388.0, "entry_high": 4394.0,
            "stop_loss": 4387.0, "tp1": 4396.0, "tp2": 4399.0, "tp3": 4403.0}


class TestFindingTheTemplateThatActuallyGoverns:
    """A channel's template can come from the channel override, the AI
    recommendation or the global strategy. Checking only the first silently
    falls through to generic distances, with nothing in the log to say the
    template was skipped."""

    def test_a_channel_override_is_found(self, monkeypatch):
        monkeypatch.setattr(ea_templates, "_strategy_override_for",
                            lambda ch: "template:GD Instituational - single")
        monkeypatch.setattr(ea_templates, "_ai_rec_for", lambda ch: None)
        monkeypatch.setattr(ea_templates, "get_ea_template", lambda n: dict(TEMPLATE))

        assert ea_templates.template_for_channel(CHANNEL, {})["name"] == TEMPLATE["name"]

    def test_the_AI_RECOMMENDATION_is_found_when_there_is_no_override(self, monkeypatch):
        """Not the live case -- that channel does have an override -- but a
        channel resolved by the recommender previously got generic distances
        with no indication that its template had been ignored."""
        monkeypatch.setattr(ea_templates, "_strategy_override_for", lambda ch: None)
        monkeypatch.setattr(ea_templates, "_ai_rec_for",
                            lambda ch: "template:GD Instituational - single")
        monkeypatch.setattr(ea_templates, "get_ea_template", lambda n: dict(TEMPLATE))

        assert ea_templates.template_for_channel(CHANNEL, {})["name"] == TEMPLATE["name"]

    def test_the_GLOBAL_strategy_is_the_last_resort(self, monkeypatch):
        monkeypatch.setattr(ea_templates, "_strategy_override_for", lambda ch: None)
        monkeypatch.setattr(ea_templates, "_ai_rec_for", lambda ch: None)
        monkeypatch.setattr(ea_templates, "get_ea_template", lambda n: dict(TEMPLATE))

        got = ea_templates.template_for_channel(
            CHANNEL, {"trade_strategy": "template:GD Instituational - single"})

        assert got["name"] == TEMPLATE["name"]

    def test_a_non_template_strategy_yields_nothing(self, monkeypatch):
        """`scale_out` is a built-in, not a template. Returning something here
        would hand every plain-strategy channel a template's distances."""
        monkeypatch.setattr(ea_templates, "_strategy_override_for", lambda ch: None)
        monkeypatch.setattr(ea_templates, "_ai_rec_for", lambda ch: None)

        assert ea_templates.template_for_channel(CHANNEL, {"trade_strategy": "scale_out"}) is None

    def test_a_lookup_that_throws_yields_nothing(self, monkeypatch):
        """This runs inside the scan loop. It must degrade to the old generic
        behaviour rather than take the scan down."""
        def _boom(ch):
            raise RuntimeError("db gone")
        monkeypatch.setattr(ea_templates, "_strategy_override_for", _boom)

        assert ea_templates.template_for_channel(CHANNEL, {}) is None


class TestTPParsingOffRespectsTheTemplate:
    from backend.src.services.telegram import keyword_triggers as _kt

    def test_a_template_asking_for_telegram_TPs_KEEPS_them(self, monkeypatch):
        """The reported bug. tp_from_telegram=1 means "use the signal's own
        targets"; the global toggle stripped them and the order was refused."""
        from backend.src.services.telegram import keyword_triggers as kt
        monkeypatch.setattr(kt.ea_templates, "template_for_channel",
                            lambda ch, rs: dict(TEMPLATE))
        parsed = _signal()

        kt.apply_tp_parsing_override(parsed, {"lk_enable_tp_parsing": 0}, CHANNEL)

        assert parsed["tp1"] == 4396.0
        assert parsed["tp2"] == 4399.0
        assert parsed["tp3"] == 4403.0

    def test_a_template_with_its_OWN_targets_supplies_them(self, monkeypatch):
        """tp_from_telegram=0: the template's pips, measured from the zone,
        exactly as the SL override measures its distance."""
        from backend.src.services.telegram import keyword_triggers as kt
        tpl = dict(TEMPLATE, tp_from_telegram=0)
        monkeypatch.setattr(kt.ea_templates, "template_for_channel", lambda ch, rs: tpl)
        parsed = _signal()

        kt.apply_tp_parsing_override(parsed, {"lk_enable_tp_parsing": 0}, CHANNEL)

        # BUY: measured up from the top of the zone, 10 pips = 1.0 point
        assert parsed["tp1"] == pytest.approx(4394.0 + 3.0)
        assert parsed["tp2"] == pytest.approx(4394.0 + 7.0)
        assert parsed["tp3"] == pytest.approx(4394.0 + 12.0)

    def test_a_sell_measures_the_other_way(self, monkeypatch):
        from backend.src.services.telegram import keyword_triggers as kt
        tpl = dict(TEMPLATE, tp_from_telegram=0)
        monkeypatch.setattr(kt.ea_templates, "template_for_channel", lambda ch, rs: tpl)
        parsed = dict(_signal(), direction="SELL")

        kt.apply_tp_parsing_override(parsed, {"lk_enable_tp_parsing": 0}, CHANNEL)

        assert parsed["tp1"] == pytest.approx(4388.0 - 3.0)

    def test_with_NO_template_the_old_stripping_stands(self, monkeypatch):
        """Unchanged for every channel not running a template."""
        from backend.src.services.telegram import keyword_triggers as kt
        monkeypatch.setattr(kt.ea_templates, "template_for_channel", lambda ch, rs: None)
        parsed = _signal()

        kt.apply_tp_parsing_override(parsed, {"lk_enable_tp_parsing": 0}, CHANNEL)

        assert all(parsed[f"tp{i}"] is None for i in (1, 2, 3))

    def test_the_toggle_ON_changes_nothing_at_all(self, monkeypatch):
        from backend.src.services.telegram import keyword_triggers as kt
        monkeypatch.setattr(kt.ea_templates, "template_for_channel", lambda ch, rs: dict(TEMPLATE))
        parsed = _signal()

        kt.apply_tp_parsing_override(parsed, {"lk_enable_tp_parsing": 1}, CHANNEL)

        assert parsed["tp1"] == 4396.0


class TestSLParsingOffFindsTheSameTemplate:
    def test_the_templates_sl_pips_is_used_not_the_generic_fallback(self, monkeypatch):
        """A governing template's own stop distance must win over the generic
        Fallback. Unit-tested with a 70-pip template because the live channel's
        template happens to specify 50, which is indistinguishable from the
        fallback and would prove nothing."""
        from backend.src.services.telegram import keyword_triggers as kt
        monkeypatch.setattr(kt.ea_templates, "template_for_channel", lambda ch, rs: dict(TEMPLATE))
        parsed = _signal()

        note = kt.apply_sl_parsing_override(parsed, {"lk_enable_sl_parsing": 0}, CHANNEL)

        assert parsed["stop_loss"] == pytest.approx(4388.0 - 7.0)
        assert "template" in note.lower()

    def test_no_template_still_falls_back(self, monkeypatch):
        from backend.src.services.telegram import keyword_triggers as kt
        monkeypatch.setattr(kt.ea_templates, "template_for_channel", lambda ch, rs: None)
        parsed = _signal()

        kt.apply_sl_parsing_override(parsed, {"lk_enable_sl_parsing": 0}, CHANNEL)

        assert parsed["stop_loss"] == pytest.approx(4388.0 - 5.0)
