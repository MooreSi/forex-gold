"""A Breakout BUY is charged the spread once on the engine's own ledger.

Owner decision, 2026-10-17 (docs/simon-handover/054, decision A).

`entry_mid` is the ASK at creation for both directions (the cycle prices
every candidate off `tick.ask`). A BUY is then closed on the BID, so the
spread is already inside its two prices; a SELL is closed on the ASK, so
neither of its prices carries one. `_compute_cost_pts` is spread + slippage
+ commission, and every leg used to be charged it. So a SELL paid the spread
once and a BUY paid it twice: about 0.03-0.04R a BUY at this engine's stops,
on the panel, the edge stats and the ML label's win/loss line.

Each figure below is checked against fills on the correct side -- a BUY in
at the ask and out at the bid, a SELL in at the bid and out at the ask --
plus 0.05 slippage. SELL figures were already right and must not move.

**Nothing here reaches a broker or a database.** The repo, the ML engine,
the cross-engine bus, the cluster ledger and the tuning ledger are recorders.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.breakout_signal import breakout_signal_manage as mg
from backend.src.services.breakout_signal import breakout_signal_service as svc

_NOW = 1_758_000_000.0
SPREAD = 0.6
SLIP = 0.05
COST = SPREAD + SLIP            # what _compute_cost_pts returns for this tick
ENTRY = 4002.0                  # entry_mid: the ask when the signal was made


class _Repo:
    def __init__(self, sig):
        self.sig = dict(sig)
        self.closed = []
        self.partials = []

    def get_signal_by_id(self, sig_id):
        return dict(self.sig)

    def close_signal(self, sig_id, close_price, outcome, note, **kw):
        self.closed.append({"close_price": close_price, "outcome": outcome, **kw})

    def book_partial_close(self, sig_id, dollars, frac, note):
        self.partials.append(dollars)
        self.sig["partial_pnl_dollars"] = self.sig.get("partial_pnl_dollars", 0.0) + dollars
        self.sig["remaining_frac"] = self.sig.get("remaining_frac", 1.0) - frac

    def set_stop_loss(self, sig_id, price):
        self.sig["stop_loss"] = price

    def move_sl_to_be(self, sig_id, be_price):
        self.sig["stop_loss"], self.sig["sl_moved_to_be"] = be_price, 1

    def fetch_main_mt5_profit(self, ticket):
        return None


class _Engine(mg._ManagementMixin):
    def __init__(self):
        self._closed_count = 0

    def _notify_refresh(self):
        pass

    async def _run_batch_analysis(self):
        pass


def _buy(**kw):
    s = {"id": 1, "signal_ref": "BO-0001", "direction": "BUY", "entry_mid": ENTRY,
         "trigger_price": ENTRY, "trigger_time": _NOW - 60, "lot_size": 0.10,
         "stop_loss": 3990.0, "tp1": 4012.15, "tp2": 4022.0, "tp3": 4030.0,
         "sl_dist": 12.0, "remaining_frac": 1.0, "partial_pnl_dollars": 0.0,
         "sl_moved_to_be": 0, "mt5_ticket": None, "strategy": "conservative",
         "created_at": _NOW - 60}
    s.update(kw)
    return s


def _sell(**kw):
    # Levels are measured from the same ask-priced entry, mirrored.
    return _buy(**{"direction": "SELL", "stop_loss": 4014.0, "tp1": 3991.85,
                   "tp2": 3982.0, "tp3": 3974.0, **kw})


@pytest.fixture
def lab(monkeypatch):
    monkeypatch.setattr(mg.time, "time", lambda: _NOW)
    monkeypatch.setattr(mg.ap, "get", lambda key: 30.0)       # time_stop_mins
    monkeypatch.setattr("backend.src.services.breakout_signal.ml_engine.record_outcome",
                        lambda sig_id, outcome: None)
    monkeypatch.setattr("backend.src.services.cluster.sync.ledger.push_trade_closed",
                        lambda payload: None)
    monkeypatch.setattr("backend.src.db.database.close_bus_entry", lambda *a: None)
    monkeypatch.setattr("backend.src.services.breakout_signal.tuning_ledger.evaluate",
                        lambda: None)

    def run(sig, bid, now=_NOW):
        repo = _Repo(sig)
        monkeypatch.setattr(mg, "bdb", repo)
        _Engine()._manage_triggered_signal(repo.get_signal_by_id(1), bid, bid + SPREAD,
                                           now, COST)
        return repo
    return run


def _true_usd(direction, exit_price_on_its_side, frac=1.0):
    """Fills on the correct side, plus slippage, at 0.10 lot ($10 a point)."""
    if direction == "BUY":
        pts = exit_price_on_its_side - ENTRY              # in at the ask, out at the bid
    else:
        pts = (ENTRY - SPREAD) - exit_price_on_its_side   # in at the bid, out at the ask
    return round((pts - SLIP) * frac * 10.0, 2)


class TestAStopLoss:
    def test_a_buy(self, lab):
        """Pinned at -126.50 before the fix: one spread ($6.00) worse
        than the fills."""
        repo = lab(_buy(), bid=3990.0)
        assert repo.closed[0]["net_pnl_dollars"] == -120.50 == _true_usd("BUY", 3990.0)

    def test_a_sell_is_unchanged(self, lab):
        repo = lab(_sell(), bid=4014.0 - SPREAD)
        assert repo.closed[0]["net_pnl_dollars"] == -126.50 == _true_usd("SELL", 4014.0)


class TestTheLastTarget:
    def test_a_buy(self, lab):
        """Pinned at 273.50 before the fix."""
        repo = lab(_buy(), bid=4030.0)
        assert repo.closed[0]["net_pnl_dollars"] == 279.50 == _true_usd("BUY", 4030.0)

    def test_a_sell_is_unchanged(self, lab):
        repo = lab(_sell(), bid=3974.0 - SPREAD)
        assert repo.closed[0]["net_pnl_dollars"] == 273.50 == _true_usd("SELL", 3974.0)


class TestTheFirstPartial:
    def test_a_buy(self, lab):
        """Pinned at 31.35 before the fix."""
        repo = lab(_buy(), bid=4012.15)
        assert repo.partials == [33.33] == [_true_usd("BUY", 4012.15, mg._TP1_FRAC)]

    def test_a_sell_is_unchanged(self, lab):
        repo = lab(_sell(), bid=3991.85 - SPREAD)
        assert repo.partials == [31.35] == [_true_usd("SELL", 3991.85, mg._TP1_FRAC)]


class TestTheSecondPartial:
    """TP1 already banked, the stop at break-even, TP2 reached."""

    def _after_tp1(self, sig):
        return dict(sig, remaining_frac=1.0 - mg._TP1_FRAC, sl_moved_to_be=1)

    def test_a_buy(self, lab):
        repo = lab(self._after_tp1(_buy(tp2=4022.15, stop_loss=4002.65)), bid=4022.15)
        assert repo.partials == [66.33] == [_true_usd("BUY", 4022.15, mg._TP2_FRAC)]

    def test_a_sell_is_unchanged(self, lab):
        repo = lab(self._after_tp1(_sell(tp2=3981.85, stop_loss=4001.35)), bid=3981.85 - SPREAD)
        assert repo.partials == [64.35] == [_true_usd("SELL", 3981.85, mg._TP2_FRAC)]


class TestTheTimeStop:
    def test_a_buy(self, lab):
        """Pinned at -76.50 before the fix."""
        repo = lab(_buy(trigger_time=_NOW - 31 * 60), bid=3995.0)
        assert repo.closed[0]["net_pnl_dollars"] == -70.50 == _true_usd("BUY", 3995.0)


class TestTheMt5SyncCharge:
    """`_check_outcomes` closes a live trade through `_close_and_learn` with
    the cost it hands over. MT5's own profit replaces the figure straight
    after, unless that lookup fails; then this figure is what stays."""

    class _Tick:
        bid, ask = 4010.0, 4010.0 + SPREAD

    class _Bridge:
        async def get_tick(self):
            return TestTheMt5SyncCharge._Tick()

    class _SyncRepo:
        def __init__(self, direction):
            self.sig = {"id": 1, "signal_ref": "BO-0001", "direction": direction,
                        "status": "triggered", "created_at": _NOW - 600,
                        "entry_mid": ENTRY, "lot_size": 0.10, "mt5_ticket": 77,
                        "live_exec_status": "success", "trigger_time": _NOW - 300}

        def get_open_signals(self):
            return [dict(self.sig)]

        def fetch_main_close_ro(self, ticket):
            return {"mt5_profit": 50.0, "net_pnl": 50.0}

    def _charged(self, monkeypatch, direction):
        seen = []

        class _E(svc.BreakoutEngine):
            def __init__(self):
                self._bridge = TestTheMt5SyncCharge._Bridge()

            def _compute_cost_pts(self, spread_raw):
                return spread_raw + SLIP

            def _close_and_learn(self, sig_id, px, outcome, note, entry, d, lot, cost):
                seen.append(cost)

            def _notify_refresh(self):
                pass

        async def _direct(fn, *a, **kw):
            return fn(*a, **kw)

        monkeypatch.setattr(svc, "bdb", self._SyncRepo(direction))
        monkeypatch.setattr(svc.time, "time", lambda: _NOW)
        monkeypatch.setattr("backend.src.db.database.to_db_thread", _direct)
        asyncio.run(_E()._check_outcomes())
        return seen

    def test_a_buy(self, monkeypatch):
        """Pinned at the full cost before the fix."""
        assert self._charged(monkeypatch, "BUY") == [pytest.approx(SLIP)]

    def test_a_sell_is_unchanged(self, monkeypatch):
        assert self._charged(monkeypatch, "SELL") == [pytest.approx(COST)]


class TestTheChargeItself:
    def test_a_buy_is_charged_everything_but_the_spread(self):
        assert mg.ledger_cost_pts("BUY", COST, SPREAD) == pytest.approx(SLIP)

    def test_a_sell_is_charged_all_of_it(self):
        assert mg.ledger_cost_pts("SELL", COST, SPREAD) == COST

    def test_a_buy_is_never_credited(self):
        """`_compute_cost_pts` falls back to a flat 0.35 when the fee settings
        cannot be read. Against a wider spread the difference would be a
        credit; it is floored at nothing instead."""
        assert mg.ledger_cost_pts("BUY", 0.35, 1.0) == 0.0
