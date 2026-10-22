"""The research symbols are found on any broker. docs/todo/broker/010.

Customer EV-156MMIO7U6F (2026-10-22) served gold and none of the seven
cross-asset peers, because its broker names them differently, and the sweep
asked again every minute. The bridge now looks the name up in the broker's
own symbol list. Read-only: nothing here sends an order, and the bridge's
trading symbol is never remapped.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import mt5_bridge
import mt5_symbols


def _sym(name, base="", profit="", visible=False):
    return SimpleNamespace(name=name, currency_base=base,
                           currency_profit=profit, visible=visible)


def _names(*names):
    return [_sym(n) for n in names]


class TestMatching:
    def test_the_exact_name_wins(self):
        syms = _names("XAGUSD", "XAGUSD.a", "SILVER")
        assert mt5_symbols.match("XAGUSD", syms) == "XAGUSD"

    @pytest.mark.parametrize("broker_name", [
        "XAGUSD.a", "XAGUSD.pro", "XAGUSDm", "XAGUSD+", "#XAGUSD", "XAGUSD_i",
    ])
    def test_an_account_type_suffix_or_prefix_is_seen_through(self, broker_name):
        syms = _names("EURUSD", broker_name)
        assert mt5_symbols.match("XAGUSD", syms) == broker_name

    @pytest.mark.parametrize("wanted, broker_name", [
        ("SP500", "US500"),
        ("SP500", "SPX500"),
        ("SP500", "US500.cash"),
        ("SP500", "US500Cash"),
        ("USDX", "DXY"),
        ("USDX", "USDIDX"),
        ("USOUSD", "USOIL"),
        ("USOUSD", "XTIUSD"),
        ("USOUSD", "WTI"),
        ("XAGUSD", "SILVER"),
        ("XPTUSD", "PLATINUM"),
        ("XAUUSD", "GOLD"),
    ])
    def test_a_known_alias_is_found(self, wanted, broker_name):
        syms = _names("EURUSD", broker_name)
        assert mt5_symbols.match(wanted, syms) == broker_name

    def test_a_pair_is_found_by_its_currencies(self):
        syms = [_sym("EURUSD", "EUR", "USD"), _sym("Silver_Spot", "XAG", "USD")]
        assert mt5_symbols.match("XAGUSD", syms) == "Silver_Spot"

    def test_the_suffix_of_the_brokers_gold_is_preferred(self):
        syms = _names("XAUUSD.a", "XAGUSD.b", "XAGUSD.a")
        assert mt5_symbols.match("XAGUSD", syms) == "XAGUSD.a"

    def test_a_symbol_already_in_market_watch_is_preferred(self):
        syms = [_sym("XAGUSD.b"), _sym("XAGUSD.c", visible=True)]
        assert mt5_symbols.match("XAGUSD", syms) == "XAGUSD.c"

    # Negative controls: a match that is wrong reads the wrong market.
    @pytest.mark.parametrize("wanted, decoy", [
        ("SP500", "US5000"),          # a different index
        ("XAUUSD", "XAUUSDX"),        # not a suffix, a longer name
        ("USDX", "DX-DEC26"),         # a dated future
        ("VIX", "VIXY"),              # an ETF, not the index
        ("XAGUSD", "XAGEUR"),         # silver in euros
    ])
    def test_a_lookalike_is_not_matched(self, wanted, decoy):
        assert mt5_symbols.match(wanted, _names("EURUSD", decoy)) is None

    def test_a_pair_with_the_wrong_quote_currency_is_not_matched(self):
        syms = [_sym("Silver_EU", "XAG", "EUR")]
        assert mt5_symbols.match("XAGUSD", syms) is None

    def test_nothing_on_the_broker_is_none(self):
        assert mt5_symbols.match("VIX", _names("EURUSD", "XAUUSD")) is None


class _Terminal:
    """A broker that names silver `XAGUSD.a` and has no S&P 500."""

    def __init__(self, symbols, server="Broker-Live"):
        self._symbols = symbols
        self.server = server
        self.symbols_get_calls = 0
        self.selected = []

    def account_info(self):
        return SimpleNamespace(server=self.server)

    def symbols_get(self):
        self.symbols_get_calls += 1
        return tuple(self._symbols)

    def symbol_info(self, name):
        return next((s for s in self._symbols if s.name == name), None)

    def symbol_select(self, name, enable):
        self.selected.append((name, enable))
        return True


@pytest.fixture(autouse=True)
def _fresh_cache():
    mt5_symbols.reset_cache()
    yield
    mt5_symbols.reset_cache()


class TestResolving:
    def test_a_name_the_broker_has_is_returned_unchanged_without_listing(self):
        term = _Terminal(_names("XAUUSD", "XAGUSD"))
        assert mt5_symbols.resolve(term, "XAGUSD") == "XAGUSD"
        assert term.symbols_get_calls == 0

    def test_a_name_it_lacks_is_mapped(self):
        term = _Terminal(_names("XAUUSD.a", "XAGUSD.a"))
        assert mt5_symbols.resolve(term, "XAGUSD") == "XAGUSD.a"

    def test_the_symbol_list_is_read_once_per_server(self):
        term = _Terminal(_names("XAUUSD.a", "XAGUSD.a", "US500"))
        for _ in range(3):
            mt5_symbols.resolve(term, "XAGUSD")
            mt5_symbols.resolve(term, "SP500")
            mt5_symbols.resolve(term, "VIX")
        assert term.symbols_get_calls == 1

    def test_a_new_server_is_looked_up_afresh(self):
        term = _Terminal(_names("XAGUSD.a"))
        assert mt5_symbols.resolve(term, "XAGUSD") == "XAGUSD.a"
        term._symbols = _names("XAGUSDm")
        term.server = "Other-Live"
        assert mt5_symbols.resolve(term, "XAGUSD") == "XAGUSDm"

    def test_no_match_hands_back_the_name_asked_for(self):
        term = _Terminal(_names("XAUUSD"))
        assert mt5_symbols.resolve(term, "VIX") == "VIX"

    def test_a_terminal_that_cannot_list_hands_back_the_name(self):
        term = _Terminal(_names("XAUUSD"))
        term.symbols_get = lambda: None
        assert mt5_symbols.resolve(term, "VIX") == "VIX"


class _BridgeTerminal(_Terminal):
    """Enough of MT5 for the bridge's two candle reads."""
    TIMEFRAME_M5 = 5

    def __init__(self, symbols, hidden=()):
        super().__init__(symbols)
        self.hidden = set(hidden)
        self.rates_for = []

    def symbol_info_tick(self, name):
        if name in self.hidden or self.symbol_info(name) is None:
            return None
        return SimpleNamespace(time=1_790_000_000)

    def symbol_select(self, name, enable):
        super().symbol_select(name, enable)
        self.hidden.discard(name)
        return True

    def copy_rates_from_pos(self, name, tf, start, count):
        self.rates_for.append(name)
        return [(1_790_000_000 - 300, 1.0, 2.0, 0.5, 1.5, 10.0)]


@pytest.fixture
def bridge(monkeypatch):
    def install(term):
        monkeypatch.setattr(mt5_bridge, "mt5", term)
        monkeypatch.setattr(mt5_bridge, "_ensure_connected", lambda: True)
        monkeypatch.setitem(mt5_bridge._TF_MAP, "M5", term.TIMEFRAME_M5)
        monkeypatch.setattr(mt5_bridge.time, "time", lambda: 1_790_000_000.0)
        return term
    return install


class TestTheBridge:
    def test_a_range_read_uses_the_brokers_name(self, bridge):
        term = bridge(_BridgeTerminal(_names("XAUUSD", "US500")))
        got = mt5_bridge._get_candles_range(1_790_000_000 - 600, 1_790_000_000,
                                            "M5", "SP500")
        assert got
        assert term.rates_for == ["US500"]

    def test_a_count_read_uses_the_brokers_name(self, bridge):
        term = bridge(_BridgeTerminal(_names("XAUUSD", "XAGUSD.a")))
        assert mt5_bridge._get_candles_for_symbol("XAGUSD", "M5", 1)
        assert term.rates_for == ["XAGUSD.a"]

    def test_the_trading_symbol_is_never_remapped(self, bridge, monkeypatch):
        """Phase 2 needs a demo session: contract size, lots and stops
        differ between a broker's gold symbols."""
        monkeypatch.setattr(mt5_bridge, "SYMBOL", "XAUUSD")
        term = bridge(_BridgeTerminal(_names("XAUUSD.a")))
        mt5_bridge._get_candles_range(1_790_000_000 - 600, 1_790_000_000,
                                      "M5", "XAUUSD")
        assert term.symbols_get_calls == 0
        assert term.rates_for == []

    def test_a_symbol_hidden_from_market_watch_is_shown_and_read(self, bridge):
        term = bridge(_BridgeTerminal(_names("XAUUSD", "USDJPY"),
                                      hidden={"USDJPY"}))
        got = mt5_bridge._get_candles_range(1_790_000_000 - 600, 1_790_000_000,
                                            "M5", "USDJPY")
        assert got
        assert term.selected == [("USDJPY", True)]

    def test_a_symbol_the_broker_lacks_is_not_selected(self, bridge):
        term = bridge(_BridgeTerminal(_names("XAUUSD")))
        got = mt5_bridge._get_candles_range(1_790_000_000 - 600, 1_790_000_000,
                                            "M5", "VIX")
        assert got is None
        assert term.selected == []
