"""parse_limit_order_signal()/is_limit_order_signal() -- the "BUY/SELL
[LIMITS] GOLD @ high/low AREA" layout (GD2 Format C3) used to trigger a
genuine EA pending order (STRATEGY_LIMIT_RUNNER), not the Python-simulated
zone-wait path every other strategy uses. See core_limit_order_signal.py."""
from forex_trader.core.signal_parser import (
    is_limit_order_signal, parse_limit_order_signal,
)

_BUY_TP_OPEN = (
    "BUY LIMITS GOLD @ 4148/4142 AREA\n\n"
    "TP 4151\n"
    "TP 4155\n"
    "TP 4160\n"
    "TP OPEN\n"
    "SL 4141"
)

_SELL_NO_OPEN = (
    "SELL GOLD @ 4180/4185\n"
    "TP 4176\n"
    "TP 4172\n"
    "SL 4189"
)


def test_is_limit_order_signal_matches_the_area_layout():
    assert is_limit_order_signal(_BUY_TP_OPEN)
    assert is_limit_order_signal(_SELL_NO_OPEN)


def test_is_limit_order_signal_false_for_unrelated_text():
    assert not is_limit_order_signal("Buy Gold 4520 - 4512\nSL 4530\nTP1 4510")
    assert not is_limit_order_signal("just some chatter about gold")


def test_parse_buy_limits_with_tp_open():
    parsed = parse_limit_order_signal(_BUY_TP_OPEN)
    assert parsed is not None
    assert parsed["direction"] == "BUY"
    assert parsed["entry_low"] == 4142.0
    assert parsed["entry_high"] == 4148.0
    assert parsed["stop_loss"] == 4141.0
    assert parsed["tp1"] == 4151.0
    assert parsed["tp2"] == 4155.0
    assert parsed["tp3"] == 4160.0
    assert parsed["tp4"] is None
    assert parsed["tp_open"] is True


def test_parse_sell_without_tp_open():
    parsed = parse_limit_order_signal(_SELL_NO_OPEN)
    assert parsed is not None
    assert parsed["direction"] == "SELL"
    assert parsed["entry_low"] == 4180.0
    assert parsed["entry_high"] == 4185.0
    assert parsed["stop_loss"] == 4189.0
    assert parsed["tp1"] == 4176.0
    assert parsed["tp2"] == 4172.0
    assert parsed["tp_open"] is False


def test_optional_limits_keyword():
    text = "BUY GOLD @ 4108/4103\nTP 4112\nTP OPEN\nSL 4098"
    parsed = parse_limit_order_signal(text)
    assert parsed is not None
    assert parsed["tp1"] == 4112.0
    assert parsed["tp_open"] is True


def test_returns_none_for_a_non_limit_order_gd2_message():
    # A totally different GD2 layout (dash-separated zone, TP1: labels) —
    # parse_gd2_signal itself would still parse it, but is_limit_order_signal
    # must gate that out since this isn't the AREA/@ layout at all.
    text = "XAU USD BUY NOW\n4150.5 - 4155.5\nTP1: 4160\nSL: 4145"
    assert not is_limit_order_signal(text)
    assert parse_limit_order_signal(text) is None


def test_returns_none_without_stop_loss_or_tps():
    # A partial message (entry only, no SL/TP yet) -- parse_gd2_partial's
    # job, not this function's.
    assert parse_limit_order_signal("BUY LIMITS GOLD @ 4148/4142 AREA") is None
