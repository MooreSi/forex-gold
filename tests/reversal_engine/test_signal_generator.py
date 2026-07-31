"""signal_generator.py's source_channel stamp -- must resolve the real
channel's CURRENT name by stable group_id, not a hardcoded literal.

Covers the 2026-07-24 fix: GD2's channel was renamed on Telegram's side
("GOLD DIGGERS 2.0 ⚡️" -> "GOLD DIGGERS INSTITUTIONAL", same group_id
2616846888) and the hardcoded stamp here kept labelling every newly
generated signal with the dead pre-rename name forever, which in turn
broke reversal_engine_correlate.py's real-signal matching (it compares
against this exact field).
"""
from backend.src.services.reversal_engine.signal_generator import (
    _current_channel_name, build_signal,
)


def test_ref_channel_resolves_by_group_id():
    assert _current_channel_name(is_gd2=False) == "Gold Diggers VIP"


def test_gd2_channel_resolves_to_current_live_name():
    # Not the dead pre-rename string -- see _TG_GROUP_ID_MAP.
    assert _current_channel_name(is_gd2=True) == "GOLD DIGGERS INSTITUTIONAL"


def test_gd2_channel_reflects_a_future_rename_with_no_code_change():
    from backend.src.services.channels import repo as cdc
    snapshot = dict(cdc._TG_GROUP_ID_MAP)
    try:
        cdc._TG_GROUP_ID_MAP["2616846888"] = "Some Future Name"
        assert _current_channel_name(is_gd2=True) == "Some Future Name"
    finally:
        cdc._TG_GROUP_ID_MAP.clear()
        cdc._TG_GROUP_ID_MAP.update(snapshot)


def test_build_signal_stamps_current_channel_name_not_hardcoded_literal():
    level = {"price": 4000.0, "score": 0.9, "type": "round_10"}
    context = {"atr": 8.0, "session": "london", "htf_bias": "bullish",
               "h1_bias": "bullish", "adx": 25.0, "price_at_signal": 4000.0}
    sig = build_signal(level, "BUY", context)
    assert sig["source_channel"] == "Gold Diggers VIP"
    assert sig["source_channel"] != "GOLD DIGGERS 2.0 ⚡️"
