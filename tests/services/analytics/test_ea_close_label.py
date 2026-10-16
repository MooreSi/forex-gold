"""A position the EA closed is not labelled "Manual".

Found 2026-10-16 on the "30 TP1 SL50 and Trail" Reversal Engine trades: 72 of
459 read "Manual" in Trade History. At 0.03 lots the template's ladder closes
0.02 at TP1 and the last 0.01 at TP3, both through the EA's own
PositionClosePartial, whose deals carry a blank comment -- and a blank comment
was read as a hand close. MT5's DEAL_REASON is what separates them: 3 is an
expert, 0/1/2 the desktop, mobile and web terminals, 4/5/6 SL, TP, stop-out.
"""
from backend.src.services.analytics import labels

EXPERT, CLIENT, MOBILE, WEB, SL, TP, SO = 3, 0, 1, 2, 4, 5, 6


def _close(comment="", reason=None, **over):
    row = {"entry": 1, "comment": comment, "time": 100.0}
    if reason is not None:
        row["reason"] = reason
    row.update(over)
    return row


def test_the_ea_closing_the_last_rung_of_a_ladder_is_an_ea_tp():
    closes = [_close(reason=EXPERT, time=100.0), _close(reason=EXPERT, time=200.0)]
    assert labels.close_reason(closes, pnl=15.0) == "EA TP"


def test_the_ea_closing_a_position_in_one_deal_is_an_ea_close_not_a_tp():
    """A single EA close is a harvest, a close-all or a final level with
    partials off; nothing in the deal says which, so it does not claim TP."""
    assert labels.close_reason([_close(reason=EXPERT)], pnl=10.0) == "EA close"


def test_a_close_from_the_terminal_is_still_manual():
    for who in (CLIENT, MOBILE, WEB):
        assert labels.close_reason([_close(reason=who)], pnl=5.0) == "Manual"


def test_a_ladder_finished_by_hand_is_manual():
    closes = [_close(reason=EXPERT, time=100.0), _close(reason=CLIENT, time=200.0)]
    assert labels.close_reason(closes, pnl=5.0) == "Manual"


def test_without_the_reason_field_the_label_is_what_it_was():
    """A bridge that has not been restarted onto the new history read sends
    no `reason`. That must not turn into a guess."""
    assert labels.close_reason([_close(), _close(time=200.0)], pnl=15.0) == "Manual"


def test_the_broker_comment_still_wins_when_there_is_one():
    closes = [_close(reason=EXPERT, time=100.0),
              _close("[sl 4197.71]", reason=SL, time=200.0)]
    assert labels.close_reason(closes, pnl=5.0) == "SL"


def test_an_app_close_keeps_its_comment_label():
    """The app's own closes are commented "close"/"partial" and are labelled
    from that, as before, though MT5 also files them as an expert."""
    assert labels.close_reason([_close("close", reason=EXPERT)], pnl=1.0) == "Manual"
    assert labels.close_reason([_close("partial", reason=EXPERT)], pnl=1.0) == "Partial TP"


def test_a_blank_comment_with_a_broker_stop_or_target_reason_says_so():
    assert labels.close_reason([_close(reason=SL)], pnl=-5.0) == "SL"
    assert labels.close_reason([_close(reason=TP)], pnl=5.0) == "TP"
    assert labels.close_reason([_close(reason=SO)], pnl=-50.0) == "Stop-out"


def test_the_last_close_decides_whatever_order_the_deals_arrive_in():
    closes = [_close(reason=EXPERT, time=300.0), _close(reason=CLIENT, time=100.0)]
    assert labels.close_reason(closes, pnl=5.0) == "EA TP"
