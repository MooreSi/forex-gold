"""A message that says LIMITS rests. The template manages it; it does not
override the entry mechanic.

docs/todo/limit-orders/010. Written RED, before the fix.

**The signal this file exists for**, live 2026-09-10 on GOLD DIGGERS
INSTITUTIONAL (channel strategy `Template: GD Instituational - single`)::

    BUY LIMITS GOLD @ 4415/4410 AREA
    TP 4418  TP 4422  TP 4427
    SL 4403

Gold was at 4428.74 -- 13.74 points ABOVE the top of a BUY zone, which is
precisely the market a buy limit is waiting for a retrace from. The app opened
a market BUY at 4428.76 and shifted SL and all three TPs up to match.

Two guards, each defensible alone, combine into that:

* `scan_messages.py:390` hands an EA Template priority over the
  format-triggered Limit Runner strategy. Correct for a **grid** template,
  which stages its own resting legs (2026-07-24).
* `scan_auto_execute.py:444` stages a pending order only when the template's
  `mode == "grid"`. A **single**-mode template therefore has no resting path
  at all -- it falls through to the market branch, where IME's gap-fire
  (`MAX_GAP_FIRE_PTS` = 15) fills anything within 15 points of its zone.

So the limit wording is parsed, believed, and then discarded.

**Why this is an end-to-end file.** The bug is not in any one function; every
function involved is doing what its own comment says. It is in which function
gets called. `tests/core/test_limit_order_signal.py` already pins the placement
itself. What is untested, and asserted here, is that a LIMITS message on a
template channel REACHES it.

**Where the fake boundary is drawn.** The EA is faked (`_FakeEA`), so
`place_pending_order` and `open_trade` are observable and countable. Everything
above it runs for real -- the Telegram reader, the parser, strategy resolution,
auto-execute -- and the broker boundary below is `FakeMT5Bridge`.

NO REAL OR DEMO ORDER CAN BE PLACED HERE -- same harness as
`test_trend_gate_end_to_end.py`: FakeMT5Bridge, FakeTelegramReader, no network,
empty bridge URL, and an EA that only records what it was asked to do.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from backend.src.runtime import TradingRuntime
from backend.src.services.broker import ea_templates
from backend.src.services.broker.fake_bridge import FakeMT5Bridge
from backend.src.services.channels.repo import set_channel_strategy_override
from backend.src.services.telegram.fake_reader import FakeTelegramReader

BASE = 1_700_000_000.0
CHANNEL = "GOLD DIGGERS INSTITUTIONAL"

# The 2026-09-10 message, verbatim in shape. entry_low 4410, entry_high 4415.
ZONE_LOW, ZONE_HIGH = 4410.0, 4415.0

BUY_LIMIT_SIGNAL = (
    "BUY LIMITS GOLD @ 4415/4410 AREA\n"
    "TP 4418\n"
    "TP 4422\n"
    "TP 4427\n"
    "SL 4403"
)

# The owner's own second example, same day. entry_low 4454, entry_high 4460,
# and a literal TP OPEN line. Present so that hardcoding "BUY" anywhere in the
# routing change survives nothing.
SELL_LIMIT_SIGNAL = (
    "SELL LIMITS GOLD @ 4454/4460 AREA\n"
    "TP 4451\n"
    "TP 4447\n"
    "TP 4442\n"
    "TP OPEN\n"
    "SL 4461"
)

# The SAME channel, the same template, a message that does NOT say LIMITS.
# This is the control against over-correction: making everything rest would
# pass every other test in this file.
BUY_MARKET_SIGNAL = (
    "Buy Gold 4425 - 4430\n"
    "Stop Loss 4403\n"
    "TP1 4440  TP2 4450  TP3 4460"
)

# Market 13.74 points above the BUY zone -- inside MAX_GAP_FIRE_PTS (15), which
# is what made the live fill happen rather than a queue.
MARKET_ABOVE_BUY_ZONE = 4428.74
# Market 14 points below the SELL zone, the mirror of the same case.
MARKET_BELOW_SELL_ZONE = 4440.00

CONFIG = {
    "starting_balance": 1000.0, "anthropic_api_key": "",
    "mt5_bridge_url": "", "mt5_native_bridge_enabled": False,
    "telegram_api_id": "", "telegram_api_hash": "",
    "sessions_dir": "./data/test_sessions",
}

SINGLE_TEMPLATE = {
    "mode": "single", "sl_pips": 70.0,
    "tp1_pips": 30.0, "tp2_pips": 70.0, "tp3_pips": 120.0,
    "tp_from_telegram": 1,
}
GRID_TEMPLATE = {
    "mode": "grid", "sl_pips": 70.0, "anchors": 1, "pendings": 3,
    "tp1_pips": 30.0, "tp2_pips": 70.0, "tp3_pips": 120.0,
}


class _FakeEA:
    """Records what it was asked to do. Placing is the whole assertion here,
    so both order calls are counted, not just spied for truthiness."""

    def __init__(self):
        self.pending_calls: list[dict] = []
        self.open_calls: list[dict] = []

    def is_ea_healthy(self) -> bool:
        return True

    def is_strategy_portable(self, strategy: str) -> bool:
        return True

    async def place_pending_order(self, trade_id, direction, price, lot_size, stop_loss,
                                  tps, pcts, be_at_pos, strategy, expire_minutes=240.0,
                                  close_full_on_last=True, trail_mode=None, template=None):
        self.pending_calls.append(dict(
            direction=direction, price=price, lot_size=lot_size, stop_loss=stop_loss,
            tps=dict(tps), strategy=strategy, template=template,
        ))
        return {"type": "pending_order_placed", "ticket": 5551}

    async def open_trade(self, trade_id, direction, lot_size, stop_loss, tps, strategy,
                         pcts=None, be_at_pos=None, trail_mode=None, template=None,
                         zone_low=None, zone_high=None, timeout=5.0):
        self.open_calls.append(dict(
            direction=direction, lot_size=lot_size, stop_loss=stop_loss,
            tps=dict(tps), strategy=strategy, template=template,
        ))
        return {"type": "trade_opened", "ticket": 7771, "fill_price": MARKET_ABOVE_BUY_ZONE}


@pytest.fixture
def ea():
    """Install a fake EA everywhere the order paths look one up."""
    fake = _FakeEA()
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake), \
         patch("backend.src.services.cluster.sync.client.get_instance", return_value=None), \
         patch("backend.src.services.cluster.sync.server.get_instance", return_value=None):
        yield fake


def _drive(fresh_db, text: str, price: float, template: dict | None,
           template_name: str = "GD Instituational - single"):
    """One signal, through the real pipeline, on a template-governed channel."""
    if template is not None:
        ea_templates.save_ea_template(template_name, dict(template))
        set_channel_strategy_override(CHANNEL, f"template:{template_name}")
    fresh_db.update_risk_settings({
        "auto_execute_signals": 1, "accept_tg_signals": 1,
        "immediate_market_entry": 1,
        # A template is managed entirely by the EA, so open_trade only ever
        # reaches the handoff with the bridge switched on (open_trade.py:471).
        "ea_bridge_enabled": 1,
    })

    engine = TradingRuntime(CONFIG)
    engine._bridge = FakeMT5Bridge(
        seed=1, scenario={"anchors": [[0, price], [300, price]]},
        base_ts=BASE, clock=lambda: BASE, starting_balance=1000.0,
    )
    reader = FakeTelegramReader(
        CONFIG, scenario={"signals": [{"at": 0, "channel": CHANNEL, "text": text}]})
    engine.set_telegram_reader(reader)
    reader.feed_due(now=1.0)
    return asyncio.run(engine._scan_messages())


def _market_fills(fresh_db) -> list[dict]:
    with fresh_db.db() as conn:
        return [fresh_db.row_to_dict(r) for r in conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE status='open'").fetchall()]


class TestTheSignalThatStartedThis:
    """BUY LIMITS 4415/4410 with gold at 4428.74."""

    def test_a_resting_order_is_placed_at_the_near_edge_of_the_zone(self, fresh_db, ea):
        _drive(fresh_db, BUY_LIMIT_SIGNAL, MARKET_ABOVE_BUY_ZONE, SINGLE_TEMPLATE)

        assert len(ea.pending_calls) == 1, (
            "a LIMITS message on a single-mode template placed no resting order"
        )
        assert ea.pending_calls[0]["direction"] == "BUY"
        assert ea.pending_calls[0]["price"] == pytest.approx(4415.00), (
            "the resting order is not at the near edge of the quoted zone"
        )

    def test_no_market_order_is_opened(self, fresh_db, ea):
        """The live failure: a market BUY at 4428.76, 13.74 points above the
        top of its own zone, with SL and every TP shifted up to match."""
        _drive(fresh_db, BUY_LIMIT_SIGNAL, MARKET_ABOVE_BUY_ZONE, SINGLE_TEMPLATE)

        assert ea.open_calls == [], (
            f"gap-fired at market instead of resting: {ea.open_calls}"
        )
        assert _market_fills(fresh_db) == []

    def test_the_stop_is_not_shifted_to_chase_the_market(self, fresh_db, ea):
        """Gap-fire moved the stop from 4403 to 4416.89 — above the top of the
        zone the trade was built around.

        The resting order's stop is the TEMPLATE's (limit-orders/020):
        `sl_pips` 70 from where the order rests, 4415.00 - 7.00 = 4408.00. That
        is the owner's rule — the keyword decides the entry, the template
        decides the management — and it is emphatically not the market being
        chased. When this file was first written, 010 alone left the signal's
        own 4403 here; 020 is what changed it, and the control below pins the
        untemplated case where 4403 does still stand."""
        _drive(fresh_db, BUY_LIMIT_SIGNAL, MARKET_ABOVE_BUY_ZONE, SINGLE_TEMPLATE)

        assert ea.pending_calls[0]["stop_loss"] == pytest.approx(4408.00)
        assert ea.pending_calls[0]["stop_loss"] < ZONE_LOW, (
            "the stop is inside or above the entry zone — the market was chased"
        )


class TestTheOtherDirection:
    """Without a SELL here, hardcoding "BUY" in the routing survives."""

    def test_a_sell_limit_rests_too(self, fresh_db, ea):
        _drive(fresh_db, SELL_LIMIT_SIGNAL, MARKET_BELOW_SELL_ZONE, SINGLE_TEMPLATE)

        assert len(ea.pending_calls) == 1, (
            "a SELL LIMITS message on a single-mode template placed no resting order"
        )
        assert ea.pending_calls[0]["direction"] == "SELL"
        assert ea.pending_calls[0]["price"] == pytest.approx(4454.00), (
            "a SELL rests at the BOTTOM of its zone -- the side price reaches first"
        )

    def test_and_opens_nothing_at_market(self, fresh_db, ea):
        _drive(fresh_db, SELL_LIMIT_SIGNAL, MARKET_BELOW_SELL_ZONE, SINGLE_TEMPLATE)

        assert ea.open_calls == []
        assert _market_fills(fresh_db) == []


class TestTheControls:
    """Without these, "make everything rest" passes every test above."""

    def test_a_message_that_does_not_say_limits_still_fills_at_market(self, fresh_db, ea):
        """Same channel, same template, no LIMITS wording. The market path is
        not what this change is about and must be untouched."""
        _drive(fresh_db, BUY_MARKET_SIGNAL, 4428.00, SINGLE_TEMPLATE)

        assert ea.pending_calls == [], (
            "an ordinary zone signal was turned into a resting order"
        )
        assert len(ea.open_calls) == 1

    def test_a_grid_template_still_stages_its_own_legs(self, fresh_db, ea):
        """A grid template IS a pending-order strategy already
        (scan_auto_execute.py:421-440). Routing it through Limit Runner would
        place one order where the user configured four."""
        _drive(fresh_db, BUY_LIMIT_SIGNAL, MARKET_ABOVE_BUY_ZONE, GRID_TEMPLATE,
               template_name="GD Instituational - grid")

        assert ea.pending_calls == [], (
            "a grid template's signal was diverted to the single Limit Runner order"
        )
        assert len(ea.open_calls) == 1

    def test_a_channel_with_no_template_is_unchanged(self, fresh_db, ea):
        """Limit Runner already handled this correctly. It must keep doing so."""
        _drive(fresh_db, BUY_LIMIT_SIGNAL, MARKET_ABOVE_BUY_ZONE, None)

        assert len(ea.pending_calls) == 1
        assert ea.open_calls == []
        assert ea.pending_calls[0]["stop_loss"] == pytest.approx(4403.00), (
            "with no template to override it, the signal's own stop stands"
        )
