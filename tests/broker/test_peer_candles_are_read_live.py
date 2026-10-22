"""Another symbol's bars come back live and stamped in true UTC.

docs/todo/reversal-engine/230. `/candles_range?symbol=` serves the
cross-asset peers. It reads by position (`copy_rates_from_pos`), and for a
symbol not in MT5's Market Watch the terminal answers from its cached
history -- hours to weeks stale, with no error. On 2026-10-22 six of the
seven peers (all but USDJPY) were stored missing on every one of 1,752
signals, because the 15-minute staleness guard in `cross_asset` refused them.

Two fixes, both pinned here:
  * a symbol other than gold is put in Market Watch before it is read;
  * the server-time offset comes from gold's quote, not the peer's. A peer
    whose market is shut (VIX and the S&P overnight) has a quote hours old,
    and an offset taken from it stamps hours-old bars as if they were now.

The fake terminal holds bars at their true UTC moment and stamps what it
returns in server time (UTC+3, the VPS's broker). Nothing here reaches a
broker: `mt5` is replaced by the fake for every test, and no order call
exists on it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import mt5_bridge
import mt5_symbols

SERVER_OFFSET = 3 * 3600
NOW = 1_790_000_000.0
BAR = 300


class _Terminal:
    """Market Watch holds gold only. A symbol outside it is served from a
    cache that ends `stale_s` ago; once selected, it is served live."""

    def __init__(self, peer_quote_age_s=2.0, stale_s=12 * 3600):
        self.selected = {mt5_bridge.SYMBOL}
        self.calls = []
        self._peer_age = peer_quote_age_s
        self._stale = stale_s

    # The resolver's lookups (docs/todo/broker/010). Every peer exists on this
    # broker under the app's own name.
    def account_info(self):
        return SimpleNamespace(server="Demo-UTC+3")

    def symbol_info(self, symbol):
        return SimpleNamespace(name=symbol, visible=symbol in self.selected)

    def symbol_select(self, symbol, enable=True):
        self.calls.append(("select", symbol, enable))
        if enable:
            self.selected.add(symbol)
        return True

    def symbol_info_tick(self, symbol):
        self.calls.append(("tick", symbol))
        age = 2.0 if symbol == mt5_bridge.SYMBOL else self._peer_age
        return SimpleNamespace(time=int(NOW - age + SERVER_OFFSET))

    def copy_rates_from_pos(self, symbol, tf, start, count):
        self.calls.append(("rates", symbol))
        end = NOW if symbol in self.selected else NOW - self._stale
        last = int(end // BAR) * BAR - BAR
        out = []
        for i in range(count):
            true_ts = last - i * BAR
            out.append((true_ts + SERVER_OFFSET, 1.0, 1.0, 1.0, 100.0 + i, 1.0))
        return list(reversed(out))


@pytest.fixture
def terminal(monkeypatch):
    def install(term):
        monkeypatch.setattr(mt5_bridge, "mt5", term)
        monkeypatch.setattr(mt5_bridge, "_ensure_connected", lambda: True)
        monkeypatch.setattr(mt5_bridge.time, "time", lambda: NOW)
        return term
    mt5_symbols.reset_cache()
    yield install
    mt5_symbols.reset_cache()


def test_a_peer_outside_market_watch_is_selected_before_it_is_read(terminal):
    term = terminal(_Terminal())

    bars = mt5_bridge._get_candles_range(NOW - 3600, NOW, "M5", "XAGUSD")

    assert ("select", "XAGUSD", True) in term.calls
    assert term.calls.index(("select", "XAGUSD", True)) < term.calls.index(("rates", "XAGUSD"))
    assert bars, "a selected peer is served live"
    assert NOW - bars[-1]["ts"] <= 2 * BAR


def test_gold_is_read_exactly_as_before(terminal):
    """Gold's path feeds the TP safety net and the max-TP checker. It gets no
    select call and takes its offset from its own quote, as it always has."""
    term = terminal(_Terminal())

    bars = mt5_bridge._get_candles_range(NOW - 3600, NOW, "M5", mt5_bridge.SYMBOL)

    assert [c for c in term.calls if c[0] == "select"] == []
    assert [c for c in term.calls if c[0] == "tick"] == [("tick", mt5_bridge.SYMBOL)]
    assert NOW - bars[-1]["ts"] <= 2 * BAR


def test_a_shut_market_is_stamped_by_golds_clock_not_its_own_quote(terminal):
    """VIX's last quote two hours old. With the offset taken from that quote,
    every bar comes back stamped two hours later than it closed."""
    terminal(_Terminal(peer_quote_age_s=2 * 3600))

    bars = mt5_bridge._get_candles_range(NOW - 3600, NOW, "M5", "VIX")

    # Seconds off the boundary is gold's own quote age, as for every read;
    # cross_asset snaps it. Hours off is the bug.
    assert bars
    assert abs(bars[-1]["ts"] - (int(NOW // BAR) * BAR - BAR)) <= 10
