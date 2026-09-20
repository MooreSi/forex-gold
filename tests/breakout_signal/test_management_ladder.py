"""The time-stop / SL / TP3 / TP2 / TP1 ladder on a triggered signal.

`_manage_triggered_signal` decides when a Breakout position is closed,
partially banked, or has its stop moved. It runs every five seconds against
the live bid and ask, and it was 28% covered.

**No test reaches a broker or a database.** `_close_and_learn` and the repo
are recorders, so every decision is observed as the call it would have made.

What matters most here is **order**, not each rung in isolation. The rungs
are checked time stop -> SL -> TP3 -> TP2 -> TP1 and each returns, so a
price that satisfies two of them resolves to the earlier one. A reordering
would not fail a per-rung test; `TestTheRungsAreCheckedInOrder` is what
catches it.

The outcome string passed to `_close_and_learn` is asserted here as the
INTENT each rung expresses. Note that the receiving function discards it
and recomputes from net P&L -- see
`test_close_and_learn_arithmetic.py::TestTheOutcomeArgumentIsDiscarded`.
These tests pin what this function means to say.
"""
from __future__ import annotations

import pytest

from backend.src.services.breakout_signal import breakout_signal_manage as mg

_NOW = 1_758_000_000.0


class _Repo:
    def __init__(self):
        self.partials = []
        self.stops = []
        self.be_moves = []

    def book_partial_close(self, sig_id, dollars, frac, note):
        self.partials.append({"sig_id": sig_id, "dollars": dollars,
                              "frac": frac, "note": note})

    def set_stop_loss(self, sig_id, price):
        self.stops.append((sig_id, price))

    def move_sl_to_be(self, sig_id, be_price):
        self.be_moves.append((sig_id, be_price))


class _Engine(mg._ManagementMixin):
    def __init__(self):
        self.closes = []
        self.refreshes = 0

    def _close_and_learn(self, sig_id, close_price, outcome, note,
                         entry, direction, lot, cost_pts):
        self.closes.append({"sig_id": sig_id, "close_price": close_price,
                            "outcome": outcome, "note": note,
                            "direction": direction})

    def _notify_refresh(self):
        self.refreshes += 1


def _sig(**kw):
    base = {
        "id": 1, "signal_ref": "BO-0001", "direction": "BUY",
        "entry_mid": 4002.0, "trigger_price": 4002.0, "lot_size": 0.10,
        "stop_loss": 3990.0, "tp1": 4010.0, "tp2": 4018.0, "tp3": 4030.0,
        "trigger_time": _NOW - 300, "remaining_frac": 1.0,
        "sl_moved_to_be": 0, "sl_dist": 12.0,
    }
    base.update(kw)
    return base


@pytest.fixture
def lab(monkeypatch):
    repo = _Repo()
    state = {"repo": repo, "time_stop_mins": 60.0}
    monkeypatch.setattr(mg, "bdb", repo)
    monkeypatch.setattr(mg.ap, "get", lambda key: state["time_stop_mins"])
    return state


def _manage(state, sig=None, bid=4004.0, ask=4004.6, now=_NOW, cost_pts=0.6):
    engine = _Engine()
    engine._manage_triggered_signal(dict(sig or _sig()), bid, ask, now, cost_pts)
    return engine, state["repo"]


# ── Nothing has happened yet ─────────────────────────────────────────────────

def test_a_position_between_its_levels_is_left_alone(lab):
    engine, repo = _manage(lab)

    assert engine.closes == []
    assert repo.partials == []
    assert repo.be_moves == []


# ── Which price each side is measured at ─────────────────────────────────────

def test_a_buy_is_measured_at_the_bid(lab):
    """A BUY is exited by selling, at the bid. Measuring it at the ask
    would skip stops by the width of the spread on every signal."""
    engine, _ = _manage(lab, bid=3990.0, ask=3990.6)

    assert engine.closes[0]["close_price"] == 3990.0


def test_a_sell_is_measured_at_the_ask(lab):
    engine, _ = _manage(lab, sig=_sig(direction="SELL", stop_loss=4014.0,
                                      tp1=3994.0, tp2=3986.0, tp3=3974.0),
                        bid=4013.4, ask=4014.0)

    assert engine.closes[0]["close_price"] == 4014.0


# ── Stop loss ────────────────────────────────────────────────────────────────

def test_a_buy_stopped_out_is_closed_as_a_loss(lab):
    engine, _ = _manage(lab, bid=3989.0, ask=3989.6)

    assert engine.closes[0]["outcome"] == "loss"
    assert "SL @" in engine.closes[0]["note"]


def test_a_sell_stopped_out_is_closed_as_a_loss(lab):
    engine, _ = _manage(lab, sig=_sig(direction="SELL", stop_loss=4014.0,
                                      tp1=3994.0, tp2=3986.0, tp3=3974.0),
                        bid=4014.4, ask=4015.0)

    assert engine.closes[0]["outcome"] == "loss"


def test_a_stop_already_moved_to_breakeven_is_not_a_loss(lab):
    """It reached TP1 and came back. Recording that as a loss would tell
    the model the setup failed when it paid."""
    engine, _ = _manage(lab, sig=_sig(sl_moved_to_be=1, stop_loss=4002.6),
                        bid=4002.0, ask=4002.6)

    assert engine.closes[0]["outcome"] == "be"


# ── Targets ──────────────────────────────────────────────────────────────────

def test_tp3_closes_the_position_as_a_win(lab):
    engine, _ = _manage(lab, bid=4031.0, ask=4031.6)

    assert engine.closes[0]["outcome"] == "win"
    assert "TP3 @" in engine.closes[0]["note"]


class TestTp1:

    def test_it_banks_a_third_and_moves_the_stop(self, lab):
        engine, repo = _manage(lab, bid=4011.0, ask=4011.6)

        assert engine.closes == [], "TP1 banks a partial, it does not close"
        assert repo.partials[0]["frac"] == mg._TP1_FRAC

    def test_the_booked_money_is_net_of_cost(self, lab):
        """9 points, less 0.6 cost, x 0.10 lots x 0.33 x 100 = $27.72."""
        _, repo = _manage(lab, bid=4011.0, ask=4011.6)

        assert repo.partials[0]["dollars"] == 27.72

    def test_the_stop_goes_to_breakeven_PLUS_the_cost(self, lab):
        """Breakeven on price is a loss on money: the round trip was still
        paid. The stop is placed where the trade actually scratches."""
        _, repo = _manage(lab, bid=4011.0, ask=4011.6)

        assert repo.be_moves == [(1, 4002.6)]

    def test_a_sell_moves_its_breakeven_stop_the_other_way(self, lab):
        _, repo = _manage(lab, sig=_sig(direction="SELL", stop_loss=4014.0,
                                        tp1=3994.0, tp2=3986.0, tp3=3974.0),
                          bid=3992.4, ask=3993.0)

        assert repo.be_moves == [(1, 4001.4)]

    def test_it_does_not_fire_twice_on_a_part_closed_position(self, lab):
        """remaining_frac below 1.0 means TP1 was already banked."""
        _, repo = _manage(lab, sig=_sig(remaining_frac=1.0 - mg._TP1_FRAC),
                          bid=4011.0, ask=4011.6)

        assert repo.partials == []


class TestTp2:

    def test_it_banks_a_second_third_and_trails_the_stop_to_tp1(self, lab):
        _, repo = _manage(lab, sig=_sig(remaining_frac=1.0 - mg._TP1_FRAC),
                          bid=4019.0, ask=4019.6)

        assert repo.partials[0]["frac"] == mg._TP2_FRAC
        assert repo.stops == [(1, 4010.0)]

    def test_it_does_not_fire_on_a_full_position(self, lab):
        """A full position at TP2 has not banked TP1 yet, so TP1 is the
        rung that owes work -- not TP2."""
        _, repo = _manage(lab, sig=_sig(remaining_frac=1.0),
                          bid=4019.0, ask=4019.6)

        assert repo.stops == []
        assert repo.partials[0]["frac"] == mg._TP1_FRAC

    def test_it_does_not_fire_again_once_two_thirds_are_banked(self, lab):
        _, repo = _manage(lab, sig=_sig(remaining_frac=1.0 - mg._TP1_FRAC
                                        - mg._TP2_FRAC),
                          bid=4019.0, ask=4019.6)

        assert repo.partials == []


# ── The time stop ────────────────────────────────────────────────────────────

class TestTheTimeStop:

    def test_a_stale_losing_position_is_closed(self, lab):
        engine, _ = _manage(lab, sig=_sig(trigger_time=_NOW - 4000),
                            bid=3998.0, ask=3998.6)

        assert "Time stop" in engine.closes[0]["note"]

    def test_a_stale_position_that_is_not_losing_is_left_open(self, lab):
        """The rule is old AND going nowhere. Time alone closes nothing."""
        engine, _ = _manage(lab, sig=_sig(trigger_time=_NOW - 4000),
                            bid=4004.0, ask=4004.6)

        assert engine.closes == []

    def test_a_recent_losing_position_is_given_time(self, lab):
        engine, _ = _manage(lab, sig=_sig(trigger_time=_NOW - 300),
                            bid=3998.0, ask=3998.6)

        assert engine.closes == []

    def test_a_part_closed_position_is_exempt(self, lab):
        """It already banked money, so it is not a trade going nowhere."""
        engine, _ = _manage(lab, sig=_sig(trigger_time=_NOW - 4000,
                                          remaining_frac=1.0 - mg._TP1_FRAC),
                            bid=3998.0, ask=3998.6)

        assert engine.closes == []

    def test_with_a_normal_stop_distance_a_time_stop_is_always_a_loss(self, lab):
        """Not a preference -- an arithmetic consequence, and the reason the
        test below exists.

        The gate is `unreal < -0.2 * sl_dist`; the "be" label needs
        `unreal >= -1.0`. For any sl_dist of 5.0 or more the gate sits at or
        below -1.0, so nothing can satisfy both and the "be" branch cannot be
        reached. XAUUSD stops are ATR-derived and comfortably past 5.0, so in
        practice every time stop reports a loss.
        """
        engine, _ = _manage(lab, sig=_sig(trigger_time=_NOW - 4000,
                                          sl_dist=12.0),
                            bid=3999.0, ask=3999.6)

        assert engine.closes[0]["outcome"] == "loss"

    def test_the_breakeven_branch_needs_a_stop_tighter_than_five_dollars(self, lab):
        """The only window where it is reachable: sl_dist 3.0 puts the gate
        at -0.6, leaving -1.0 <= unreal < -0.6 for "be"."""
        engine, _ = _manage(lab, sig=_sig(trigger_time=_NOW - 4000,
                                          sl_dist=3.0),
                            bid=4001.2, ask=4001.8)

        assert engine.closes[0]["outcome"] == "be"


# ── Order ────────────────────────────────────────────────────────────────────

class TestTheRungsAreCheckedInOrder:
    """Each rung returns, so a price satisfying two resolves to the earlier
    one. Per-rung tests cannot see a reordering; these can."""

    # There is deliberately no "stop and target on the same bar" test: this
    # function evaluates ONE price, and on a normal ladder (stop below entry,
    # targets above) no single price can satisfy both. The orderings that can
    # actually conflict are the two below plus TP3 over TP1/TP2.

    def test_the_time_stop_is_checked_before_the_stop_loss(self, lab):
        engine, _ = _manage(lab, sig=_sig(trigger_time=_NOW - 4000),
                            bid=3989.0, ask=3989.6)

        assert "Time stop" in engine.closes[0]["note"]

    def test_tp3_wins_over_tp1_when_price_gapped_past_both(self, lab):
        engine, repo = _manage(lab, bid=4031.0, ask=4031.6)

        assert engine.closes[0]["outcome"] == "win"
        assert repo.partials == [], "a gap to TP3 closes out, it does not part-bank"

    def test_tp3_wins_over_tp2_on_a_part_closed_position(self, lab):
        engine, repo = _manage(lab, sig=_sig(remaining_frac=1.0 - mg._TP1_FRAC),
                               bid=4031.0, ask=4031.6)

        assert engine.closes[0]["outcome"] == "win"
        assert repo.partials == []
