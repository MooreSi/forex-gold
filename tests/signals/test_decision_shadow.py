"""Champion and challenger for Telegram decisions (stage 1).

Expected behaviour, from docs/todo/signal-validation/010:

  * The champion is what actually happened. Without it in the table there is
    nothing to compare against and a list of challengers proves nothing --
    reversal_engine/shadow.py's rule, and the reason it keeps its own
    champion row.
  * An unavailable fact ABSTAINS. A challenger that stood aside on a fact it
    never had would report a refusal rate that says nothing about the gate,
    and it would be read as though it did.
  * Nothing here decides anything. Every variant is a recording.
  * The HTF bias is the one fact that cannot be had for free at decision
    time, so it is read in the background sweep -- and only while it is
    still contemporaneous. Past that it abstains rather than scoring a
    different moment.
"""
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.services.signals import decision_shadow as shadow
from backend.src.services.signals import decision_log_repo as repo
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from tests.conftest import remove_db_file


@pytest.fixture
def log_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    repo.create_schema()
    yield repo
    re_repo.close_db()
    remove_db_file(path)


def _variant(name):
    return next(v for v in shadow.DEFAULT_VARIANTS if v.name == name)


def _row(**over):
    row = {"executed": 1, "liquidity_blocked": 0, "event_blocked": 0,
           "spread_points": 20.0, "direction": "BUY",
           "decided_at": time.time(), "tg_message_id": "1", "path": "auto",
           "channel_name": "Gold Diggers VIP"}
    row.update(over)
    return row


class TestTheChampionIsWhatHappened:
    def test_it_exists(self):
        champs = [v for v in shadow.DEFAULT_VARIANTS if v.is_champion]
        assert len(champs) == 1

    def test_it_reports_an_execution_as_taken(self):
        take, _ = shadow.decide(_variant("live (champion)"), _row(executed=1))
        assert take is True

    def test_it_reports_a_block_as_not_taken(self):
        take, _ = shadow.decide(_variant("live (champion)"), _row(executed=0))
        assert take is False

    def test_it_ignores_every_gate_fact(self):
        """The champion is a record, not a rule. It must not start
        disagreeing with reality because a challenger's fact is set."""
        take, _ = shadow.decide(_variant("live (champion)"),
                                _row(executed=1, liquidity_blocked=1,
                                     event_blocked=1, spread_points=900.0))
        assert take is True


class TestTheChallengers:
    def test_session_liquidity_stands_aside_inside_a_window(self):
        take, reason = shadow.decide(_variant("session liquidity"),
                                     _row(liquidity_blocked=1))
        assert take is False
        assert reason

    def test_session_liquidity_takes_it_otherwise(self):
        take, _ = shadow.decide(_variant("session liquidity"),
                                _row(liquidity_blocked=0))
        assert take is True

    def test_the_event_tier_variant_stands_aside_near_an_event(self):
        take, _ = shadow.decide(_variant("event tier"), _row(event_blocked=1))
        assert take is False

    def test_the_spread_guard_stands_aside_on_a_wide_spread(self):
        take, _ = shadow.decide(_variant("spread guard"), _row(spread_points=80.0))
        assert take is False

    def test_the_spread_guard_takes_a_normal_spread(self):
        take, _ = shadow.decide(_variant("spread guard"), _row(spread_points=20.0))
        assert take is True

    def test_a_challenger_never_takes_what_the_live_path_never_offered(self):
        """A blocked decision has no fill to inherit. A challenger claiming
        it 'would have taken' one is claiming a trade that never existed."""
        take, reason = shadow.decide(_variant("session liquidity"),
                                     _row(executed=0, liquidity_blocked=0))
        assert take is False
        assert "not executed" in reason.lower()


class TestAnUnavailableFactAbstains:
    def test_a_missing_liquidity_fact_is_None_not_False(self):
        take, _ = shadow.decide(_variant("session liquidity"),
                                _row(liquidity_blocked=None))
        assert take is None

    def test_a_missing_spread_is_None_not_False(self):
        take, _ = shadow.decide(_variant("spread guard"), _row(spread_points=None))
        assert take is None

    def test_an_abstention_is_stored_as_NULL(self, log_db):
        did = repo.insert_decision({"tg_message_id": "x", "path": "auto",
                                    "decided_at": time.time(), "executed": 1})
        shadow.record_all(did, _row(liquidity_blocked=None))
        stored = {r["variant"]: r["would_take"] for r in repo.shadow_rows_for(did)}
        assert stored["session liquidity"] is None


@pytest.mark.asyncio
class TestTheBiasIsOnlyScoredWhileItIsStillTheSameMoment:
    async def test_a_fresh_decision_gets_a_bias_read(self, log_db):
        did = repo.insert_decision({"tg_message_id": "fresh", "path": "auto",
                                    "decided_at": time.time(), "executed": 1,
                                    "direction": "BUY"})
        with mock.patch.object(shadow, "_bias_blocks",
                               new=mock.AsyncMock(return_value=True)):
            await shadow.evaluate_pending(bridge=object())
        stored = {r["variant"]: r["would_take"] for r in repo.shadow_rows_for(did)}
        assert stored["trend (HTF bias)"] == 0

    async def test_a_STALE_decision_abstains_rather_than_scoring_a_later_moment(self, log_db):
        """The bias read happens now; the decision happened then. Past the
        window those are different moments, and a number from the wrong
        moment is worse than no number."""
        old = time.time() - (shadow.MAX_BIAS_LAG_S + 60)
        did = repo.insert_decision({"tg_message_id": "stale", "path": "auto",
                                    "decided_at": old, "executed": 1,
                                    "direction": "BUY"})
        with mock.patch.object(shadow, "_bias_blocks",
                               new=mock.AsyncMock(return_value=True)) as biased:
            await shadow.evaluate_pending(bridge=object())
        stored = {r["variant"]: r["would_take"] for r in repo.shadow_rows_for(did)}
        assert stored["trend (HTF bias)"] is None
        assert biased.await_count == 0, "a stale row must not even ask"

    async def test_every_decision_is_evaluated_once_and_then_left_alone(self, log_db):
        did = repo.insert_decision({"tg_message_id": "once", "path": "auto",
                                    "decided_at": time.time(), "executed": 1})
        with mock.patch.object(shadow, "_bias_blocks",
                               new=mock.AsyncMock(return_value=None)):
            await shadow.evaluate_pending(bridge=object())
            await shadow.evaluate_pending(bridge=object())
        assert len(repo.shadow_rows_for(did)) == len(shadow.DEFAULT_VARIANTS)
        assert repo.unevaluated_shadow() == []

    async def test_a_failing_bias_read_does_not_stop_the_sweep(self, log_db):
        did = repo.insert_decision({"tg_message_id": "boom", "path": "auto",
                                    "decided_at": time.time(), "executed": 1,
                                    "liquidity_blocked": 0})
        with mock.patch.object(shadow, "_bias_blocks",
                               new=mock.AsyncMock(side_effect=RuntimeError("no bridge"))):
            await shadow.evaluate_pending(bridge=object())
        stored = {r["variant"]: r["would_take"] for r in repo.shadow_rows_for(did)}
        assert stored["trend (HTF bias)"] is None
        assert stored["session liquidity"] is not None, \
            "one broken fact must not cost the others"


@pytest.mark.asyncio
class TestTheReport:
    async def test_a_variant_with_nothing_scored_reports_None_not_zero(self, log_db):
        """Zero expectancy and no evidence are different statements, and a
        table rendering both as 0.000 invites the wrong one to be acted on."""
        rows = {r["variant"]: r for r in shadow.report()}
        assert rows["session liquidity"]["mean_r"] is None
        assert rows["session liquidity"]["n_taken"] == 0

    async def test_it_scores_only_the_trades_a_variant_would_have_taken(self, log_db):
        winner = repo.insert_decision({"tg_message_id": "w", "path": "auto",
                                       "decided_at": time.time(), "executed": 1,
                                       "liquidity_blocked": 0})
        loser = repo.insert_decision({"tg_message_id": "l", "path": "auto",
                                      "decided_at": time.time(), "executed": 1,
                                      "liquidity_blocked": 1})
        repo.set_outcome(winner, outcome="win", net_usd=100.0, realised_r=2.0,
                         max_tp_hit="TP2", exit_reason="TP")
        repo.set_outcome(loser, outcome="loss", net_usd=-50.0, realised_r=-1.0,
                         max_tp_hit="none", exit_reason="SL")
        for did, row in ((winner, _row(liquidity_blocked=0)),
                         (loser, _row(liquidity_blocked=1))):
            shadow.record_all(did, row)

        rows = {r["variant"]: r for r in shadow.report()}
        champion = rows["live (champion)"]
        challenger = rows["session liquidity"]
        assert champion["n_taken"] == 2
        assert champion["net"] == 50.0
        assert challenger["n_taken"] == 1
        assert challenger["net"] == 100.0, "it stood aside from the loser"
        assert challenger["mean_r"] == 2.0


@pytest.mark.asyncio
class TestTheEntryTriggerReplay:
    """The trigger reads M1 micro-structure, so it is only honest against
    candles as of the decision instant. Scored from a candles_range replay
    ending at decided_at -- never from a live read, which would be a
    different moment presented as the same one.

    Unlike the bias, it therefore has NO freshness limit: a replay of last
    Tuesday is exactly as honest as a replay of one minute ago.
    """

    async def test_it_replays_candles_ENDING_at_the_decision(self, log_db):
        asked = {}

        async def _range(from_ts, to_ts, timeframe="M1"):
            asked.update(from_ts=from_ts, to_ts=to_ts, timeframe=timeframe)
            return []

        decided = time.time() - 86400 * 3
        repo.insert_decision({"tg_message_id": "replay", "path": "auto",
                              "decided_at": decided, "executed": 1,
                              "direction": "BUY", "entry_low": 4100.0,
                              "entry_high": 4102.0, "price": 4101.0})
        bridge = SimpleNamespace(get_candles_range=_range)
        await shadow.evaluate_pending(bridge=bridge)

        assert asked["to_ts"] == pytest.approx(decided)
        assert asked["from_ts"] < decided, "the window must end at the decision"
        assert asked["timeframe"] == "M1"

    async def test_an_old_decision_is_still_scored(self, log_db):
        """The freshness rule belongs to the bias, which reads live. A
        replay does not have that problem and must not inherit its limit."""
        decided = time.time() - 86400 * 3
        did = repo.insert_decision({"tg_message_id": "old", "path": "auto",
                                    "decided_at": decided, "executed": 1,
                                    "direction": "BUY", "price": 4101.0})
        with mock.patch.object(shadow, "_trigger_blocks",
                               new=mock.AsyncMock(return_value=True)):
            await shadow.evaluate_pending(bridge=object())
        stored = {r["variant"]: r["would_take"] for r in repo.shadow_rows_for(did)}
        assert stored["confirmed entry"] == 0

    async def test_no_candles_means_ABSTAIN_not_blocked(self, log_db):
        """A check that could not run is not a check that failed --
        reversal_engine_live_execute's rule for the same gate."""
        did = repo.insert_decision({"tg_message_id": "nocandles", "path": "auto",
                                    "decided_at": time.time(), "executed": 1,
                                    "direction": "BUY", "price": 4101.0})

        async def _empty(from_ts, to_ts, timeframe="M1"):
            return []

        await shadow.evaluate_pending(bridge=SimpleNamespace(get_candles_range=_empty))
        stored = {r["variant"]: r["would_take"] for r in repo.shadow_rows_for(did)}
        assert stored["confirmed entry"] is None

    async def test_a_decision_with_no_level_abstains(self, log_db):
        """A bare IME direction has no entry zone and the row may have no
        price either. There is nothing to confirm against."""
        did = repo.insert_decision({"tg_message_id": "nolevel", "path": "ime",
                                    "decided_at": time.time(), "executed": 1,
                                    "direction": "BUY"})
        await shadow.evaluate_pending(bridge=object())
        stored = {r["variant"]: r["would_take"] for r in repo.shadow_rows_for(did)}
        assert stored["confirmed entry"] is None

    async def test_the_entry_MID_is_the_level_when_the_signal_has_a_zone(self):
        assert shadow._level_for({"entry_low": 4100.0, "entry_high": 4102.0,
                                  "price": 4090.0}) == 4101.0

    async def test_the_decision_price_is_the_level_when_it_has_none(self):
        assert shadow._level_for({"price": 4090.0}) == 4090.0
