"""The strategy catalogue the AI Market Analysis recommends from.

The behaviour that matters: a recommendation must be able to name ANY
option the user actually has -- every pre-coded strategy, every custom
strategy, and every EA template, including one the user created minutes
ago. Before this existed the prompt hardcoded six built-ins, so a template
could never be recommended and eleven built-ins were invisible.

No order is placed, modified or cancelled anywhere in this file; the AI
provider is faked, so nothing leaves the process.
"""
import asyncio
import json
import os
import tempfile
from unittest import mock

import pytest

from backend.src.services.ai import claude_ai
from backend.src.services.broker import ea_templates as et
from backend.src.services.positions import core_strategy_catalogue as cat
from backend.src.db import database as db
from backend.src.utils.models import STRATEGY_NAMES


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


def _key_of(entries, key):
    return next((e for e in entries if e["key"] == key), None)


# ── What the catalogue contains ────────────────────────────────────────────

def test_every_precoded_strategy_is_offered(fresh_db):
    entries = cat.build_catalogue()

    keys = {e["key"] for e in entries if e["kind"] == cat.KIND_BUILTIN}
    assert keys == set(STRATEGY_NAMES)


def test_precoded_strategy_carries_its_own_description(fresh_db):
    entries = cat.build_catalogue()

    fixed_rr = _key_of(entries, "fixed_rr")
    assert fixed_rr["label"] == "Fixed R:R"
    assert "one stop, one target" in fixed_rr["summary"]
    assert "**" not in fixed_rr["summary"]


def test_template_created_by_the_user_is_offered_under_its_override_key(fresh_db):
    et.save_ea_template("Sniper Grid", {"mode": "grid"})

    entries = cat.build_catalogue()

    tpl = _key_of(entries, "template:Sniper Grid")
    assert tpl is not None
    assert tpl["kind"] == cat.KIND_TEMPLATE
    assert tpl["label"] == "Template: Sniper Grid"


def test_template_saved_after_a_previous_build_is_picked_up(fresh_db):
    before = cat.build_catalogue()
    assert _key_of(before, "template:Late Addition") is None

    et.save_ea_template("Late Addition", {"mode": "single"})

    after = cat.build_catalogue()
    assert _key_of(after, "template:Late Addition") is not None


def test_template_summary_describes_its_configuration_not_its_name(fresh_db):
    et.save_ea_template("Sniper Grid", {
        "mode": "grid", "pending_mode": "step", "grid_step_pts": 12.0,
        "anchors": 2, "pendings": 3, "lot_anchor": 0.05, "lot_pending": 0.02,
        "sl_pips": 40.0,
        "tp1_pips": 30.0, "tp1_pct": 60.0, "tp2_pips": 80.0, "tp2_pct": 40.0,
        "trail_mode": "candle", "trail_distance": 25.0, "trail_step": 5.0,
        "trail_activation": 60.0,
        "be_mode": "entry_buffer", "be_buffer_pts": 2.0, "be_trigger": 2,
        "sig_guard": True, "sig_guard_pips": 20.0, "max_spread_pips": 6.0,
    })

    summary = _key_of(cat.build_catalogue(), "template:Sniper Grid")["summary"]

    assert "grid" in summary
    assert "2 anchor leg(s) at market (0.05 lot)" in summary
    assert "3 pending leg(s) (0.02 lot)" in summary
    assert "stepped 12 pts apart" in summary
    assert "SL 40 pips" in summary
    assert "TP ladder 30/80 pips at 60/40%" in summary
    assert "candle trail 25 pips, step 5, armed at 60" in summary
    assert "breakeven at TP2 +2 pts" in summary
    assert "sig guard 20 pips" in summary
    assert "max spread 6 pips" in summary


def test_template_taking_its_targets_from_the_signal_says_so(fresh_db):
    et.save_ea_template("Follow The Channel", {
        "tp_from_telegram": True, "tp1_pips": 30.0, "tp1_pct": 100.0,
    })

    summary = _key_of(cat.build_catalogue(), "template:Follow The Channel")["summary"]

    assert "TP levels taken from the signal message" in summary
    assert "TP ladder" not in summary


def test_atr_sized_template_reports_atr_rather_than_fixed_sl(fresh_db):
    et.save_ea_template("ATR Single", {
        "use_dynamic_atr": True, "atr_period": 20, "atr_sl_mult": 2.0,
        "sl_pips": 40.0,
    })

    summary = _key_of(cat.build_catalogue(), "template:ATR Single")["summary"]

    assert "SL from ATR(20) x 2" in summary
    assert "SL 40 pips" not in summary


def test_custom_strategy_is_offered_with_its_description(fresh_db):
    db.save_custom_strategy({
        "id": "custom_1", "name": "Tight Scalp",
        "description": "**Tight Scalp** — 3pt stop, one target.\nMore detail here.",
        "base_strategy": "scale_out", "rules_json": "{}", "created_at": 1_754_000_000.0,
    })

    entry = _key_of(cat.build_catalogue(), "custom_1")

    assert entry["kind"] == cat.KIND_CUSTOM
    assert entry["label"] == "Tight Scalp"
    assert entry["summary"] == "Tight Scalp — 3pt stop, one target."


def test_hidden_strategies_are_not_offered(fresh_db):
    db.set_app_config("hidden_strategies", json.dumps(["be_runner", "custom_1"]))
    db.save_custom_strategy({
        "id": "custom_1", "name": "Hidden One", "description": "x",
        "base_strategy": "scale_out", "rules_json": "{}", "created_at": 1_754_000_000.0,
    })

    entries = cat.build_catalogue()

    assert _key_of(entries, "be_runner") is None
    assert _key_of(entries, "custom_1") is None
    assert _key_of(entries, "scale_out") is not None


def test_hidden_strategies_are_still_resolvable_for_display(fresh_db):
    db.set_app_config("hidden_strategies", json.dumps(["be_runner"]))

    entries = cat.build_catalogue(include_hidden=True)

    assert _key_of(entries, "be_runner") is not None


# ── Prompt rendering ───────────────────────────────────────────────────────

def test_prompt_lines_name_every_offered_key(fresh_db):
    et.save_ea_template("Sniper Grid", {"mode": "grid"})
    entries = cat.build_catalogue()

    text = "\n".join(cat.prompt_lines(entries))

    for key in cat.valid_keys(entries):
        assert key in text


def test_prompt_lines_separate_templates_from_builtins(fresh_db):
    et.save_ea_template("Sniper Grid", {"mode": "grid"})

    text = "\n".join(cat.prompt_lines(cat.build_catalogue()))

    assert "Built-in strategies" in text
    assert "EA templates" in text


# ── Resolving what the model wrote back ────────────────────────────────────

def test_resolve_key_accepts_an_exact_key(fresh_db):
    entries = cat.build_catalogue()

    assert cat.resolve_key("trail_stop", entries) == "trail_stop"


def test_resolve_key_maps_a_bare_template_name_onto_its_key(fresh_db):
    et.save_ea_template("Sniper Grid", {"mode": "grid"})
    entries = cat.build_catalogue()

    assert cat.resolve_key("Sniper Grid", entries) == "template:Sniper Grid"


def test_resolve_key_maps_a_display_label_onto_its_key(fresh_db):
    entries = cat.build_catalogue()

    assert cat.resolve_key("Trend Ratchet", entries) == "no_sl_scale"


def test_resolve_key_rejects_a_strategy_the_user_does_not_have(fresh_db):
    entries = cat.build_catalogue()

    assert cat.resolve_key("martingale_recovery", entries) is None


# ── The analysis call itself ───────────────────────────────────────────────

_ANALYSIS_JSON = (
    '{"sentiment":"bullish","sentiment_confidence":0.7,"today_bias":"up",'
    '"price_low":4000.0,"price_high":4050.0,"key_drivers":["USD"],'
    '"technical_summary":"t","support_levels":[4000.0],"resistance_levels":[4050.0],'
    '"strategy_recommendation":"%s","strategy_reason":"r","risk_factors":[],'
    '"signal_analysis":"-","summary":"s","disclaimer":"d"}'
)


def _run_analysis(reply: str, strategies):
    """Call the real request_market_analysis with a faked provider reply.

    News fetch is stubbed because it is a live HTTP call to Yahoo Finance.
    """
    sent: dict = {}

    async def _fake_complete(cfg, system, prompt, max_tokens, timeout=30):
        sent["system"] = system
        sent["prompt"] = prompt
        return reply

    async def _no_news():
        return []

    with mock.patch.object(claude_ai.ai_provider, "is_configured", return_value=True), \
         mock.patch.object(claude_ai.ai_provider, "complete", _fake_complete), \
         mock.patch.object(claude_ai, "_fetch_gold_news", _no_news):
        data = asyncio.run(claude_ai.request_market_analysis(
            tick=None, candles=[], recent_signals=[], performance={},
            cfg={"ai_provider": "claude", "claude_api_key": "k"},
            strategies=strategies,
        ))
    return data, sent


def test_analysis_prompt_offers_the_users_templates(fresh_db):
    et.save_ea_template("Sniper Grid", {"mode": "grid", "lot_anchor": 0.05})
    strategies = cat.build_catalogue()

    _data, sent = _run_analysis(_ANALYSIS_JSON % "scale_out", strategies)

    assert "template:Sniper Grid" in sent["prompt"]
    assert "signal_climber" in sent["prompt"]
    assert "0.05 lot" in sent["prompt"]


def test_analysis_builds_the_catalogue_itself_when_the_caller_omits_it(fresh_db):
    et.save_ea_template("Sniper Grid", {"mode": "grid"})

    _data, sent = _run_analysis(_ANALYSIS_JSON % "scale_out", None)

    assert "template:Sniper Grid" in sent["prompt"]


def test_template_recommendation_survives_and_is_labelled(fresh_db):
    et.save_ea_template("Sniper Grid", {"mode": "grid"})
    strategies = cat.build_catalogue()

    data, _sent = _run_analysis(_ANALYSIS_JSON % "template:Sniper Grid", strategies)

    assert data["strategy_recommendation"] == "template:Sniper Grid"
    assert data["strategy_label"] == "Template: Sniper Grid"
    assert "grid" in data["strategy_summary"]


def test_template_recommended_by_bare_name_is_normalised_to_its_key(fresh_db):
    et.save_ea_template("Sniper Grid", {"mode": "grid"})
    strategies = cat.build_catalogue()

    data, _sent = _run_analysis(_ANALYSIS_JSON % "Sniper Grid", strategies)

    assert data["strategy_recommendation"] == "template:Sniper Grid"


def test_recommendation_the_user_does_not_have_falls_back_to_scale_out(fresh_db):
    strategies = cat.build_catalogue()

    data, _sent = _run_analysis(_ANALYSIS_JSON % "martingale_recovery", strategies)

    assert data["strategy_recommendation"] == "scale_out"
    assert data["strategy_label"] == "Scale Out + Breakeven"


def test_fallback_never_names_a_strategy_the_user_hid(fresh_db):
    db.set_app_config("hidden_strategies", json.dumps(["scale_out"]))
    strategies = cat.build_catalogue()

    data, _sent = _run_analysis(_ANALYSIS_JSON % "martingale_recovery", strategies)

    assert data["strategy_recommendation"] != "scale_out"
    assert data["strategy_recommendation"] in cat.valid_keys(strategies)
