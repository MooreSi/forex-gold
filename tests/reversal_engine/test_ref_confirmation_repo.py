"""Two lookups moved into reversal_engine_repo.

The existing REF-confirmation suite already pins the enabled-channel filter,
the time window, the price proximity and the direction match. It does not pin
which of several matches is returned, and the trade lookup in
reversal_engine_manage was not covered at all -- replacing its WHERE clause
with `signal_id IS NOT NULL` left the suite green.
"""
from __future__ import annotations

from backend.src.services.reversal_engine import reversal_engine_repo as re_db


def _channel(conn, name, enabled=1):
    conn.execute(
        "INSERT OR REPLACE INTO channel_parser_config (channel_name, enabled) "
        "VALUES (?,?)", (name, enabled))


def _sig(conn, tg_id, parsed_at, mid=4000.0, direction="BUY", group="chan"):
    conn.execute(
        "INSERT INTO vantage_tg_signals (tg_message_id, group_id, group_name, "
        "raw_text, parsed_at, direction, entry_low, entry_high) "
        "VALUES (?, 'g1', ?, 'raw', ?, ?, ?, ?)",
        (tg_id, group, parsed_at, direction, mid - 1.0, mid + 1.0))


class TestFindConfirmingSignal:
    def test_returns_the_newest_of_several_matches(self, fresh_db):
        """If a channel posted twice inside the window, the later message is
        the one that reflects what it currently thinks. Taking the older one
        would confirm a live trade against a view the channel has moved on
        from."""
        with fresh_db.db() as conn:
            _channel(conn, "chan")
            _sig(conn, "older", 1000.0)
            _sig(conn, "newer", 1500.0)
        got = re_db.find_confirming_signal("BUY", 4000.0, 500.0, 2000.0, 3.0)
        assert got["tg_message_id"] == "newer"

    def test_a_signal_with_no_entry_zone_is_not_a_match(self, fresh_db):
        """Excluded rather than treated as a match at price 0."""
        with fresh_db.db() as conn:
            _channel(conn, "chan")
            conn.execute(
                "INSERT INTO vantage_tg_signals (tg_message_id, group_id, group_name, "
                "raw_text, parsed_at, direction) VALUES ('x','g1','chan','raw',1000.0,'BUY')")
        assert re_db.find_confirming_signal("BUY", 4000.0, 500.0, 2000.0, 3.0) is None

    def test_no_match_is_none(self, fresh_db):
        with fresh_db.db() as conn:
            _channel(conn, "chan")
        assert re_db.find_confirming_signal("BUY", 4000.0, 500.0, 2000.0, 3.0) is None


class TestFetchTradeIdAndStrategyForSignal:
    def _trade(self, conn, trade_id, signal_id, strategy="template:grid"):
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?, 'BUY', 4000, 4001, 3990, 'active', 1)",
            (signal_id,))
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "open_time, status, strategy) VALUES (?,?,'BUY',4000,4001,4000.5,0.01,0.01,"
            "3990,1,'open',?)", (trade_id, signal_id, strategy))

    def test_returns_the_trade_for_that_signal(self, fresh_db):
        with fresh_db.db() as conn:
            self._trade(conn, "t1", "s1")
        assert re_db.fetch_trade_id_and_strategy_for_signal("s1") == ("t1", "template:grid")

    def test_does_not_return_another_signals_trade(self, fresh_db):
        """The caller uses the trade_id to build the EA comment prefix it then
        reconciles broker legs against. The wrong trade attributes another
        position's legs to this signal."""
        with fresh_db.db() as conn:
            self._trade(conn, "t1", "s1")
            self._trade(conn, "t2", "s2")
        assert re_db.fetch_trade_id_and_strategy_for_signal("s2") == ("t2", "template:grid")

    def test_an_unknown_signal_is_a_none_pair(self, fresh_db):
        """(None, None), not a raise: the caller unpacks two values and bails
        on a falsy trade_id."""
        assert re_db.fetch_trade_id_and_strategy_for_signal("nope") == (None, None)
