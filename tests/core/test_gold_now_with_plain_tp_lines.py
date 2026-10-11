"""bugs/061, second gap (owner 2026-10-11: "investigate and fix").

Gold Diggers Scalping posts "Buy/Sell Gold Now", an entry range, plain
"TP <price>" lines and "SL <price>". The "Gold Now" reader (C2) only looked
for targets under a "Targets" header, so this complete signal never parsed
in full: the live scan recorded it as a partial awaiting a follow-up that the
message itself already contained. On the demo database, 26 such messages
since 2026-09-08 sat in `pending_followup` and were never traded; the
reference backfill read none of them.

C2 now falls back to the plain "TP <price>" lines (the C3 reader) when there
is no Targets header. "TP open" is filler and is skipped, as in C3.

A full parse takes the route the Targets layout already takes: auto-execute
first offers it to `find_and_apply_instant_followup`, so it cannot open a
second trade beside an Immediate Market Entry already opened for it.

Parser only. Nothing here reaches a broker.
"""
from backend.src.services.signals import parser

SELL = ("Sell Gold Now \n\n4191 - 4197\n\nTP 4188.5\nTP 4186.5\nTP 4184\n"
        "TP 4182\nTP open\n\nSL 4201")
HIGH_RISK_SELL = ("High Risk \n\nSell Gold Now \n\n4182 - 4188\n\nTP 4180\n"
                  "TP 4178\nTP 4176\nTP 4174\nTP open\n\nSL 4193")
TARGETS = ("Buy Gold Now\n4163.5 - 4158.5\nTargets\n4165.5\n4167.5\n4170\n"
           "SL/ invalid 4155.5")


def test_the_scalping_layout_parses_as_a_full_signal():
    p = parser.parse_gd2_signal(SELL)
    assert p is not None
    assert p["direction"] == "SELL"
    assert (p["entry_low"], p["entry_high"]) == (4191.0, 4197.0)
    assert p["stop_loss"] == 4201.0
    assert [p[f"tp{i}"] for i in range(1, 5)] == [4188.5, 4186.5, 4184.0, 4182.0]
    assert p["tp5"] is None, "'TP open' is not a price"


def test_a_risk_line_above_the_direction_does_not_stop_it():
    p = parser.parse_gd2_signal(HIGH_RISK_SELL)
    assert p is not None and p["stop_loss"] == 4193.0 and p["tp1"] == 4180.0


def test_the_targets_layout_is_unchanged():
    p = parser.parse_gd2_signal(TARGETS)
    assert [p["tp1"], p["tp2"], p["tp3"]] == [4165.5, 4167.5, 4170.0]
    assert p["stop_loss"] == 4155.5


def test_without_any_target_it_is_still_not_a_full_signal():
    assert parser.parse_gd2_signal("Sell Gold Now\n\n4191 - 4197\n\nSL 4201") is None
