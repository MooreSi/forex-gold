"""The AI Trade Analysis page's data gathering.

527 lines that were moved verbatim out of `frontend/pages/ai_trade_analysis.py`
and never tested. They were *executed* by NiceGUI page render tests, which is
why coverage looked reasonable, but nothing asserted a single derived number.

That matters more here than in most repos, because what this module produces is
fed to Claude and then read by the owner as evidence about how the channels are
performing. A wrong win rate or a mislabelled phantom TP does not crash
anything — it argues for a decision.

The behaviours pinned are the ones the code is *for*:

* phantom TP detection (a channel claiming TP on a trade that stopped out);
* the reply-chain scan that finds those claims, and breakeven instructions;
* R:R, entry drift and the simulated 50%-at-TP1 comparison;
* the DPM/fixed split, which the docstring says is mutually exclusive.

`time.time()` is patched in every test that reads a window. The module computes
`since_ts = time.time() - days * 86400` at call time, so without it these tests
pass or fail by how long the rest of the suite took to get here.

Nothing here can reach a broker: the only I/O is the temporary SQLite file
`fresh_db` creates, opened by path.
"""
from __future__ import annotations

import pytest

from backend.src.db import database as db_module
from backend.src.services.analytics import ai_analysis_repo as repo

from ._seed import (
    DAY, NOW, closed_trade, dpm_perf, partial_close, signal, tg_reply,
    tg_signal, trade,
)


@pytest.fixture
def db_path(fresh_db):
    """The file `fresh_db` built. This module takes a path, not a connection —
    the page analyses a chosen environment's database by path."""
    path = db_module._DB_PATH
    assert path, "fresh_db did not record a database path"
    return path


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    """`since_ts` is computed from the wall clock inside the call."""
    monkeypatch.setattr(repo.time, "time", lambda: NOW + 2 * DAY)


def _only_channel(db_path, days: int = 30) -> dict:
    channels = repo._gather_channel_data(db_path, days)
    assert len(channels) == 1, f"expected one channel, got {len(channels)}"
    return channels[0]


# ── Sessions ─────────────────────────────────────────────────────────────────

class TestSessionFromTimestamp:
    """The session buckets, by UTC hour. Every boundary is tested because the
    breakdown they feed is read as "this channel loses money in the Asian
    session" — an off-by-one hour moves losses between sessions and the number
    still looks plausible."""

    @pytest.mark.parametrize("hour,expected", [
        (0, "Asian"), (6, "Asian"),
        (7, "London"), (11, "London"),
        (12, "London/NY"), (15, "London/NY"),
        (16, "NY"), (20, "NY"),
        (21, "Off-hours"), (23, "Off-hours"),
    ])
    def test_the_hour_decides_the_session(self, hour, expected):
        import datetime as _dt

        ts = _dt.datetime(2026, 6, 15, hour, 30, tzinfo=_dt.timezone.utc).timestamp()

        assert repo._session_from_ts(ts) == expected

    def test_no_timestamp_is_unknown_rather_than_a_guess(self):
        assert repo._session_from_ts(None) == "Unknown"
        assert repo._session_from_ts(0) == "Unknown"


# ── Which signals are gathered at all ────────────────────────────────────────

class TestChannelSelection:
    def test_a_signal_with_no_parsed_direction_is_not_a_signal(self, db_path):
        """A Telegram message the parser could not read a direction from is
        commentary, not a signal. Counting it would dilute every rate the page
        reports."""
        tg_signal(1, direction=None)

        assert repo._gather_channel_data(db_path, 30) == []

    def test_a_signal_older_than_the_window_is_excluded(self, db_path):
        tg_signal(1, parsed_at=NOW - 60 * DAY)

        assert repo._gather_channel_data(db_path, 30) == []

    def test_channels_come_back_one_dict_each_ordered_by_name(self, db_path):
        tg_signal(1, group_id="-2", group_name="Zulu Signals")
        tg_signal(2, group_id="-1", group_name="Alpha Signals")

        names = [c["channel_name"] for c in repo._gather_channel_data(db_path, 30)]

        assert names == ["Alpha Signals", "Zulu Signals"]

    def test_a_channel_with_no_name_falls_back_to_its_id(self, db_path):
        """Otherwise the page renders a blank row that cannot be told from a
        bug."""
        tg_signal(1, group_id="-100999", group_name=None)

        assert _only_channel(db_path)["channel_name"] == "-100999"


# ── Reply chain and what is claimed in it ────────────────────────────────────

class TestClaimedOutcomes:
    def test_replies_are_collected_in_message_order(self, db_path):
        tg_signal(1)
        tg_reply(20, reply_to=1, text="second")
        tg_reply(10, reply_to=1, text="first")

        chain = _only_channel(db_path)["signals"][0]["reply_chain"]

        assert [r["text"] for r in chain] == ["first", "second"]

    def test_a_reply_to_a_different_message_is_not_collected(self, db_path):
        """Negative control on the join. Without the reply_to filter every
        message in the channel would be attributed to every signal."""
        tg_signal(1)
        tg_reply(10, reply_to=999, text="about something else")

        assert _only_channel(db_path)["signals"][0]["reply_chain"] == []

    def test_claimed_tps_are_extracted_deduplicated_and_sorted(self, db_path):
        tg_signal(1)
        tg_reply(10, reply_to=1, text="TP2 hit 🎯")
        tg_reply(11, reply_to=1, text="TP1 reached")
        tg_reply(12, reply_to=1, text="TP2 hit again")

        assert _only_channel(db_path)["signals"][0]["claimed_tps"] == [1, 2]

    def test_a_reply_with_no_claim_sets_nothing(self, db_path):
        """Negative control: the pattern must not match ordinary chatter."""
        tg_signal(1)
        tg_reply(10, reply_to=1, text="watching this one closely")

        s = _only_channel(db_path)["signals"][0]
        assert s["claimed_tps"] == []
        assert s["claimed_sl"] is False

    @pytest.mark.parametrize("text", [
        "stop loss hit", "SL triggered", "stopped out — SL HIT", "STOP HIT",
    ])
    def test_a_stop_loss_acknowledgement_is_recognised(self, db_path, text):
        tg_signal(1)
        tg_reply(10, reply_to=1, text=text)

        assert _only_channel(db_path)["signals"][0]["claimed_sl"] is True

    def test_a_breakeven_instruction_is_flagged_for_the_channel(self, db_path):
        """The page reports whether a channel tells followers to move to
        breakeven, because it changes what its published win rate means."""
        tg_signal(1)
        tg_reply(10, reply_to=1, text="Move SL to entry now, risk free")

        assert _only_channel(db_path)["stats"]["be_instructions_in_channel"] is True

    def test_a_channel_that_never_says_it_is_not_flagged(self, db_path):
        tg_signal(1)
        tg_reply(10, reply_to=1, text="holding")

        assert _only_channel(db_path)["stats"]["be_instructions_in_channel"] is False


# ── Phantom TPs ──────────────────────────────────────────────────────────────

class TestPhantomTpDetection:
    """A channel claiming "TP1 hit" on a trade that actually stopped out. This
    is the number the page exists to surface, so each of its three conditions
    gets its own test rather than one combined case."""

    def test_a_claimed_tp_on_a_trade_that_stopped_out_is_a_phantom(self, db_path):
        tg_signal(1, signal_id="sig-1")
        closed_trade("t1", signal_id="sig-1", net_pnl=-18.40, exit_reason="SL")
        tg_reply(10, reply_to=1, text="TP1 hit ✅")

        assert _only_channel(db_path)["signals"][0]["is_phantom"] is True

    def test_a_claimed_tp_on_a_losing_trade_is_a_phantom_whatever_the_exit_says(self, db_path):
        tg_signal(1, signal_id="sig-1")
        closed_trade("t1", signal_id="sig-1", net_pnl=-3.10, exit_reason="Manual")
        tg_reply(10, reply_to=1, text="TP1 hit")

        assert _only_channel(db_path)["signals"][0]["is_phantom"] is True

    def test_a_claimed_tp_on_a_winning_trade_is_not_a_phantom(self, db_path):
        tg_signal(1, signal_id="sig-1")
        closed_trade("t1", signal_id="sig-1", net_pnl=42.00, exit_reason="TP")
        tg_reply(10, reply_to=1, text="TP1 hit")

        assert _only_channel(db_path)["signals"][0]["is_phantom"] is False

    def test_a_claim_with_no_trade_of_ours_is_not_a_phantom(self, db_path):
        """We cannot contradict a claim about a signal we never took.

        Worth knowing how this passes. The code carries an explicit
        `s.get("trade_id") and  # must have a real trade` condition, and
        deleting it does NOT make this test fail — because the LEFT JOIN
        leaves `exit_reason` NULL and `net_pnl` NULL for an untaken signal, so
        the third condition is False on its own. The guard is belt-and-braces,
        not load-bearing, and that was established by mutation rather than
        assumed. The assertion below is still the one that matters: whatever
        the implementation, a claim we cannot contradict is not a phantom.
        """
        tg_signal(1, signal_id="sig-1")
        tg_reply(10, reply_to=1, text="TP1 hit")

        assert _only_channel(db_path)["signals"][0]["is_phantom"] is False

    def test_a_losing_trade_with_no_claim_is_not_a_phantom(self, db_path):
        """A loss is not a phantom. Without the claim condition every SL would
        be counted as one and the number would be meaningless."""
        tg_signal(1, signal_id="sig-1")
        closed_trade("t1", signal_id="sig-1", net_pnl=-18.40, exit_reason="SL")

        assert _only_channel(db_path)["signals"][0]["is_phantom"] is False

    def test_phantoms_are_counted_in_the_channel_stats(self, db_path):
        tg_signal(1, signal_id="sig-1")
        closed_trade("t1", signal_id="sig-1", net_pnl=-18.40, exit_reason="SL")
        tg_reply(10, reply_to=1, text="TP1 hit ✅")

        assert _only_channel(db_path)["stats"]["phantom_tp_count"] == 1


# ── Derived numbers ──────────────────────────────────────────────────────────

class TestDerivedMetrics:
    def test_risk_reward_is_measured_from_the_entry_midpoint(self, db_path):
        """Entry 2430-2432 (mid 2431), SL 2421 (10 away), TP1 2441 (10 away)
        and TP2 2451 (20 away) give 1.0 and 2.0."""
        tg_signal(1, entry_low=2430.0, entry_high=2432.0, stop_loss=2421.0,
                  tp1=2441.0, tp2=2451.0)

        s = _only_channel(db_path)["signals"][0]

        assert s["rr_tp1"] == 1.0
        assert s["rr_last"] == 2.0

    def test_risk_reward_flips_sign_for_a_sell(self, db_path):
        """A SELL's targets are below entry. Without the sign flip every sell
        signal reports a negative R:R and the channel looks suicidal."""
        tg_signal(1, direction="SELL", entry_low=2430.0, entry_high=2432.0,
                  stop_loss=2441.0, tp1=2421.0)

        assert _only_channel(db_path)["signals"][0]["rr_tp1"] == 1.0

    def test_risk_reward_is_absent_when_there_is_no_stop(self, db_path):
        """No stop means no risk to divide by. Reporting 0.0 would read as a
        measured value."""
        tg_signal(1, stop_loss=None)

        s = _only_channel(db_path)["signals"][0]

        assert s["rr_tp1"] is None
        assert s["rr_last"] is None

    def test_entry_drift_measures_the_fill_against_the_zone_midpoint(self, db_path):
        """Entry zone 2430-2432 (mid 2431), filled at 2432.5 — 1.5 away, which
        the page reports in pips."""
        tg_signal(1, signal_id="sig-1", entry_low=2430.0, entry_high=2432.0)
        closed_trade("t1", signal_id="sig-1", net_pnl=10.0, entry_price=2432.5)

        assert _only_channel(db_path)["signals"][0]["entry_drift_pips"] == 15.0

    def test_the_simulated_half_at_tp1_model_prices_both_halves(self, db_path):
        """Half the lot at TP1, half at the real close. 0.10 lots, entry 2431,
        TP1 2441, closed 2436: 0.05*100*(10) + 0.05*100*(5) = 50 + 25."""
        tg_signal(1, signal_id="sig-1", entry_low=2430.0, entry_high=2432.0, tp1=2441.0)
        closed_trade("t1", signal_id="sig-1", net_pnl=25.0,
                     entry_price=2431.0, close_price=2436.0, lot_size=0.10)

        assert _only_channel(db_path)["signals"][0]["simulated_50pct_tp1_pnl"] == 75.0

    def test_the_simulated_model_is_absent_without_a_close(self, db_path):
        """An open trade has no second half to price."""
        tg_signal(1, signal_id="sig-1")
        trade("t1", signal_id="sig-1", status="open")

        assert _only_channel(db_path)["signals"][0]["simulated_50pct_tp1_pnl"] is None

    def test_partial_closes_are_attached_to_their_trade(self, db_path):
        tg_signal(1, signal_id="sig-1")
        closed_trade("t1", signal_id="sig-1", net_pnl=10.0)
        partial_close("t1", lots=0.05, price=2440.0, pnl=45.0)

        pcs = _only_channel(db_path)["signals"][0]["trade"]["partial_closes"]

        assert len(pcs) == 1
        assert pcs[0]["lots_closed"] == 0.05
        assert pcs[0]["pnl"] == 45.0


# ── Channel-level statistics ─────────────────────────────────────────────────

class TestChannelStats:
    def test_win_rate_counts_only_closed_trades(self, db_path):
        """An open trade has no outcome yet. Counting it as a loss (or a win)
        moves the rate the owner reads as evidence."""
        tg_signal(1, signal_id="sig-1")
        tg_signal(2, signal_id="sig-2")
        tg_signal(3, signal_id="sig-3")
        closed_trade("t1", signal_id="sig-1", net_pnl=30.0)
        closed_trade("t2", signal_id="sig-2", net_pnl=-10.0, exit_reason="SL")
        signal("sig-3")
        trade("t3", signal_id="sig-3", status="open")

        stats = _only_channel(db_path)["stats"]

        assert stats["total_signals"] == 3
        assert stats["signals_with_trades"] == 3
        assert stats["closed_trades"] == 2
        assert stats["wins"] == 1
        assert stats["sl_hits"] == 1
        assert stats["win_rate_pct"] == 50.0
        assert stats["total_pnl"] == 20.0

    def test_a_channel_with_no_closed_trades_reports_zero_not_a_crash(self, db_path):
        """`len(closed)` is a divisor."""
        tg_signal(1)

        stats = _only_channel(db_path)["stats"]

        assert stats["win_rate_pct"] == 0.0
        assert stats["total_pnl"] == 0.0

    def test_the_longest_losing_streak_is_reported_not_the_current_one(self, db_path):
        """Three losses, a win, then one loss: the answer is 3, not 1."""
        for i, pnl in enumerate([-5.0, -6.0, -7.0, 20.0, -8.0], start=1):
            tg_signal(i, signal_id=f"sig-{i}")
            closed_trade(f"t{i}", signal_id=f"sig-{i}", net_pnl=pnl,
                         close_time=NOW + 3600 + i)

        assert _only_channel(db_path)["stats"]["max_consecutive_losses"] == 3

    def test_profit_is_split_by_the_session_the_signal_arrived_in(self, db_path):
        import datetime as _dt

        london = _dt.datetime(2026, 6, 15, 9, 0, tzinfo=_dt.timezone.utc).timestamp()
        ny = _dt.datetime(2026, 6, 15, 18, 0, tzinfo=_dt.timezone.utc).timestamp()
        tg_signal(1, signal_id="sig-1", parsed_at=london)
        tg_signal(2, signal_id="sig-2", parsed_at=ny)
        closed_trade("t1", signal_id="sig-1", net_pnl=30.0)
        closed_trade("t2", signal_id="sig-2", net_pnl=-12.5, exit_reason="SL")

        assert _only_channel(db_path)["stats"]["session_pnl"] == {
            "London": 30.0, "NY": -12.5,
        }


# ── Fixed strategies vs DPM ──────────────────────────────────────────────────

class TestStrategyVersusDpm:
    def test_the_two_groups_are_mutually_exclusive(self, db_path):
        """The module's own claim: "Fixed-strategy stats use only trades NOT in
        that table". If a DPM trade leaked into the fixed side it would be
        counted twice and the comparison the page exists to draw would be
        between overlapping sets."""
        signal("sig-1"); signal("sig-2")
        closed_trade("dpm1", signal_id="sig-1", net_pnl=15.0)
        closed_trade("fix1", signal_id="sig-2", net_pnl=-5.0, exit_reason="SL")
        dpm_perf("dpm1")

        out = repo._gather_strategy_dpm_data(db_path, 30)

        assert out["total_closed"] == 2
        assert out["dpm_stats"]["count"] == 1
        assert out["fixed_stats"]["count"] == 1
        assert out["dpm_stats"]["total_pnl"] == 15.0
        assert out["fixed_stats"]["total_pnl"] == -5.0

    def test_an_all_winning_group_does_not_divide_by_zero(self, db_path):
        """`max(loss_p, 0.01)` is the guard. Without it a flawless strategy
        crashes the page that was about to praise it."""
        signal("sig-1")
        closed_trade("fix1", signal_id="sig-1", net_pnl=40.0)

        out = repo._gather_strategy_dpm_data(db_path, 30)

        assert out["fixed_stats"]["profit_factor"] == 4000.0

    def test_an_empty_window_reports_zeros_rather_than_failing(self, db_path):
        out = repo._gather_strategy_dpm_data(db_path, 30)

        assert out["total_closed"] == 0
        assert out["fixed_stats"] == {
            "count": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "avg_pnl": 0.0, "profit_factor": 0.0,
            "avg_hold_min": 0, "sl_exits": 0, "be_exits": 0,
        }

    def test_trades_are_broken_down_per_fixed_strategy(self, db_path):
        signal("sig-1"); signal("sig-2")
        closed_trade("a", signal_id="sig-1", net_pnl=10.0, strategy="be_runner")
        closed_trade("b", signal_id="sig-2", net_pnl=-4.0, strategy="trail_stop",
                     exit_reason="SL")

        out = repo._gather_strategy_dpm_data(db_path, 30)

        assert [s["strategy"] for s in out["strategy_breakdown"]] == [
            "be_runner", "trail_stop",
        ]
        assert out["strategy_breakdown"][0]["total_pnl"] == 10.0
        assert out["strategy_breakdown"][1]["sl_exits"] == 1

    def test_hold_time_is_reported_in_minutes(self, db_path):
        signal("sig-1")
        closed_trade("a", signal_id="sig-1", net_pnl=10.0,
                     open_time=NOW, close_time=NOW + 90 * 60)

        assert repo._gather_strategy_dpm_data(db_path, 30)["fixed_stats"]["avg_hold_min"] == 90

    def test_dpm_detail_summarises_the_exit_and_regime_mix(self, db_path):
        signal("sig-1"); signal("sig-2")
        closed_trade("d1", signal_id="sig-1", net_pnl=15.0)
        closed_trade("d2", signal_id="sig-2", net_pnl=-5.0)
        dpm_perf("d1", r_multiple=2.0, exit_type="trail", peak_pnl=20.0,
                 final_pnl=15.0, regime="trend", used_calibrated=1)
        dpm_perf("d2", r_multiple=-1.0, exit_type="sl", peak_pnl=2.0,
                 final_pnl=-5.0, regime="range", used_calibrated=0)

        detail = repo._gather_strategy_dpm_data(db_path, 30)["dpm_detail"]

        assert detail["count"] == 2
        assert detail["avg_r_multiple"] == 0.5
        assert detail["exit_breakdown"]["trail"] == {"count": 1, "pnl": 15.0, "wins": 1}
        assert detail["exit_breakdown"]["sl"] == {"count": 1, "pnl": -5.0, "wins": 0}
        assert detail["regime_breakdown"].keys() == {"trend", "range"}
        assert detail["calibrated_trades"] == 1
        assert detail["uncalibrated_trades"] == 1

    def test_trail_capture_only_counts_profitable_trailed_exits(self, db_path):
        """"how much of the peak did the trail keep" is meaningless for a trade
        that ended negative, and for one that never trailed."""
        signal("sig-1"); signal("sig-2")
        closed_trade("d1", signal_id="sig-1", net_pnl=15.0)
        closed_trade("d2", signal_id="sig-2", net_pnl=-5.0)
        dpm_perf("d1", exit_type="trail", peak_pnl=20.0, final_pnl=15.0)
        dpm_perf("d2", exit_type="sl", peak_pnl=20.0, final_pnl=-5.0)

        detail = repo._gather_strategy_dpm_data(db_path, 30)["dpm_detail"]

        assert detail["avg_trail_capture"] == 0.75

    def test_trail_capture_is_absent_when_nothing_trailed_into_profit(self, db_path):
        """None, not 0.0 — "no data" and "kept none of the peak" are different
        answers and the page renders them differently."""
        signal("sig-1")
        closed_trade("d1", signal_id="sig-1", net_pnl=-5.0)
        dpm_perf("d1", exit_type="sl", peak_pnl=20.0, final_pnl=-5.0)

        assert repo._gather_strategy_dpm_data(db_path, 30)["dpm_detail"]["avg_trail_capture"] is None
