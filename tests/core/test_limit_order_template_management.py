"""A resting order's template decides its stop, its targets and its size —
measured from the price it will actually fill at.

docs/todo/limit-orders/020. Written RED, before the fix.

Once [010](../../docs/todo/limit-orders/010-limit-keywords-beat-the-template.md)
routes a LIMITS message on a template channel to `handle_limit_order_signal`,
`_resolve_management` is reached with a template override — and today it
explicitly refuses one:

    if is_template_override(override):
        # Should be unreachable -- engine.py routes template channels away
        # from this module entirely -- ...
        return default

That comment stops being true. Left as-is, the order rests and then fills under
Limit Runner's own even-split ladder and the generic `risk_per_trade_pct`
sizing, ignoring everything the template says. That is exactly the mismatch
bugs/023 found on the Immediate Market Entry path, where a template's `sl_pips`
was silently replaced by a generic ATR placeholder.

**The reference price is the whole difficulty.** `resolution.py:540-576`
measures a template's SL from `tick.ask`/`tick.bid`, and `resolve_template_tps`
measures its TP ladder from the same place. Both are right for a market order,
which fills at the tick. A resting order fills at the price being named now,
which may be an hour and many points away — so the same code applied unchanged
puts the distance right and the level wrong.

Every test below therefore places the tick **deliberately far** from the
resting price: zone 4410–4415 with the market at 4428.74, the live 2026-09-10
geometry. A tick-referenced implementation cannot pass any of them by accident.

Owner decision, 2026-09-10 (docs/todo/limit-orders/QUESTIONS.md #1): measured
from the resting price.

No broker is reachable here — the EA is a fake that records its arguments.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.src.services.broker import ea_templates
from backend.src.services.channels.parser_repo import save_channel_parser_config
from backend.src.services.channels.repo import set_channel_strategy_override
from backend.src.services.positions.core_pips import PIPS_TO_PRICE_XAUUSD
from backend.src.services.trading import limit_order_signal as los
from backend.src.utils.models import Tick
from tests._fakes import _FakeBridge

CHANNEL = "GOLD DIGGERS INSTITUTIONAL"
TEMPLATE_NAME = "GD Instituational - single"

# The 2026-09-10 zone. A BUY rests at the TOP of its zone — the side price
# reaches first coming down.
ZONE_LOW, ZONE_HIGH = 4410.0, 4415.0
RESTING_PRICE = ZONE_HIGH

# The market when that signal arrived: 13.74 points above the top of the zone.
# Far enough that every tick-referenced level is unmistakably wrong.
MARKET_BID, MARKET_ASK = 4428.24, 4428.74

# sl_pips 60 -> 6.00 in price. From the resting price that is 4409.00.
# From the tick it would be 4422.74, which is above the entry: not a stop at all.
SL_PIPS = 60.0
EXPECTED_SL = 4409.00
SL_IF_MEASURED_FROM_THE_TICK = 4422.74

# Chosen so no expected TP collides with the signal's own stated 4418/4422/4427
# — otherwise a test could pass while the template was being ignored.
TP_PIPS = (40.0, 90.0, 150.0)
EXPECTED_TPS = {1: 4419.00, 2: 4424.00, 3: 4430.00}


def _template(**over):
    t = {
        "mode": "single",
        "sl_pips": SL_PIPS,
        "tp1_pips": TP_PIPS[0], "tp2_pips": TP_PIPS[1], "tp3_pips": TP_PIPS[2],
        "tp_from_telegram": 0,
        "risk_pct": 0.0,
        "lot_anchor": 0.02,
        "use_dynamic_atr": 0,
    }
    t.update(over)
    return t


class _FakeEA:
    def __init__(self):
        self.calls: list[dict] = []

    def is_ea_healthy(self) -> bool:
        return True

    async def place_pending_order(self, trade_id, direction, price, lot_size, stop_loss,
                                  tps, pcts, be_at_pos, strategy, expire_minutes=240.0,
                                  close_full_on_last=True, trail_mode=None, template=None):
        self.calls.append(dict(
            direction=direction, price=price, lot_size=lot_size, stop_loss=stop_loss,
            tps=dict(tps), pcts=list(pcts), strategy=strategy, template=template,
            close_full_on_last=close_full_on_last,
        ))
        return {"type": "pending_order_placed", "ticket": 5551}


def _bridge_at(bid=MARKET_BID, ask=MARKET_ASK):
    """The shared fake, holding a tick far from where the order will rest.

    A sixteenth local `_FakeBridge` would have been the obvious thing to write
    and is what the fixture-dedup ratchet exists to stop; `tests/_fakes.py`
    gained an optional tick instead.
    """
    return _FakeBridge(tick=Tick(
        bid=bid, ask=ask, mid=(bid + ask) / 2, spread=ask - bid,
        spread_points=(ask - bid) * 100, timestamp=0.0, source="fake"))


def _parsed(direction="BUY", tp_open=False):
    """The 2026-09-10 message, parsed. Its own TPs are present and are NOT
    what the template will use — `tp_from_telegram` is off."""
    return {
        "direction": direction,
        "entry_low": ZONE_LOW, "entry_high": ZONE_HIGH,
        "stop_loss": 4403.0,
        "tp1": 4418.0, "tp2": 4422.0, "tp3": 4427.0,
        "tp4": None, "tp5": None, "tp6": None, "tp7": None, "tp8": None,
        "tp_open": tp_open,
    }


async def _balance():
    return 1000.0


def _recording_lot_size(calls):
    def _lot(entry, sl, balance, risk_pct):
        calls.append(dict(entry=entry, sl=sl, balance=balance, risk_pct=risk_pct))
        # Deliberately neither `lot_anchor` (0.02) nor `max_lot_size` (0.10):
        # a risk-sized lot that happened to equal either would make the two
        # anchor tests below pass on this fake's return value.
        return 0.07
    return _lot


async def _place(fresh_db, template=None, parsed=None, rs=None, lot_calls=None,
                 bridge=None):
    """Run one placement on a template-governed channel and return the fake EA."""
    # `is_telegram_source` -- which decides whether `tp_from_telegram` can
    # take the message's own levels -- answers from `channel_parser_config`,
    # not from a name pattern. scan_messages.py auto-bootstraps a row the
    # first time it sees a channel, so in production every real channel has
    # one; a unit test calling handle_limit_order_signal directly has to seed
    # it, or the channel silently is not a Telegram channel at all.
    save_channel_parser_config(CHANNEL, "generic", "", False, True, "")
    if template is not None:
        ea_templates.save_ea_template(TEMPLATE_NAME, dict(template))
        set_channel_strategy_override(CHANNEL, f"template:{TEMPLATE_NAME}")
    ea = _FakeEA()
    rs = rs if rs is not None else {"risk_per_trade_pct": 0.5, "strategy_lot_size": 0,
                                    "max_lot_size": 0.10, "lk_entry_realignment": 0}
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=ea):
        await los.handle_limit_order_signal(
            parsed or _parsed(), "tg1", CHANNEL, CHANNEL, rs,
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance,
            suggest_lot_size_fn=_recording_lot_size(lot_calls if lot_calls is not None else []),
            bridge=bridge if bridge is not None else _bridge_at(),
        )
    return ea


class TestTheStop:
    @pytest.mark.asyncio
    async def test_the_stop_comes_from_the_templates_sl_pips(self, fresh_db):
        ea = await _place(fresh_db, _template())

        assert len(ea.calls) == 1, "no resting order was placed"
        assert ea.calls[0]["stop_loss"] == pytest.approx(EXPECTED_SL), (
            "the resting order kept the signal's own 4403 stop instead of the "
            "template's 60 pips"
        )

    @pytest.mark.asyncio
    async def test_it_is_measured_from_the_resting_price_not_the_tick(self, fresh_db):
        """The tick sits 13.74 points above the resting price. Measuring from it
        would put a BUY's 'stop' at 4422.74 — seven points ABOVE the entry."""
        ea = await _place(fresh_db, _template())

        assert ea.calls[0]["stop_loss"] == pytest.approx(EXPECTED_SL)
        assert ea.calls[0]["stop_loss"] != pytest.approx(SL_IF_MEASURED_FROM_THE_TICK), (
            "the stop was measured from the current tick, not the resting price"
        )
        assert ea.calls[0]["stop_loss"] < ea.calls[0]["price"], (
            "a BUY's stop is below its entry"
        )

    @pytest.mark.asyncio
    async def test_a_sell_measures_the_other_way(self, fresh_db):
        """Without this, `-` hardcoded in place of the direction sign survives."""
        parsed = _parsed(direction="SELL")
        ea = await _place(fresh_db, _template(), parsed=parsed)

        # A SELL rests at the BOTTOM of its zone; its stop is above.
        assert ea.calls[0]["price"] == pytest.approx(ZONE_LOW)
        assert ea.calls[0]["stop_loss"] == pytest.approx(
            ZONE_LOW + SL_PIPS * PIPS_TO_PRICE_XAUUSD)

    @pytest.mark.asyncio
    async def test_sl_pips_zero_defers_to_the_signals_own_stop(self, fresh_db):
        """Unchanged from `resolution.py`: an unset template stop is not an
        instruction to invent one."""
        ea = await _place(fresh_db, _template(sl_pips=0.0))

        assert ea.calls[0]["stop_loss"] == pytest.approx(4403.0)


class TestTheTargets:
    @pytest.mark.asyncio
    async def test_the_ladder_comes_from_the_templates_pips(self, fresh_db):
        ea = await _place(fresh_db, _template())

        assert ea.calls[0]["tps"] == pytest.approx(EXPECTED_TPS), (
            "the resting order carried the signal's own 4418/4422/4427 instead "
            "of the template's ladder"
        )

    @pytest.mark.asyncio
    async def test_the_ladder_is_measured_from_the_resting_price_too(self, fresh_db):
        """SL and TP must share one entry reference — `resolution.py`'s own
        comment says so. A tick-referenced ladder here would be 4432.74 /
        4437.74 / 4443.74."""
        ea = await _place(fresh_db, _template())

        tick_referenced = {n: round(MARKET_ASK + p * PIPS_TO_PRICE_XAUUSD, 2)
                           for n, p in enumerate(TP_PIPS, start=1)}
        assert ea.calls[0]["tps"] == pytest.approx(EXPECTED_TPS)
        assert ea.calls[0]["tps"] != pytest.approx(tick_referenced), (
            "the TP ladder was measured from the current tick, not the resting price"
        )

    @pytest.mark.asyncio
    async def test_tp_from_telegram_keeps_the_messages_own_levels(self, fresh_db):
        """40 of this channel's earlier trades ran under a template with this
        flag set. It must keep winning."""
        ea = await _place(fresh_db, _template(tp_from_telegram=1))

        assert ea.calls[0]["tps"] == pytest.approx({1: 4418.0, 2: 4422.0, 3: 4427.0})

    @pytest.mark.asyncio
    async def test_a_tp_open_line_still_reserves_the_runner(self, fresh_db):
        """The reserve is a property of the SIGNAL, not of the ladder — the
        existing `_resolve_management` docstring says so, and a template must
        not quietly close the runner leg."""
        ea = await _place(fresh_db, _template(), parsed=_parsed(tp_open=True))

        assert ea.calls[0]["close_full_on_last"] is False, (
            "a TP OPEN signal's last target closed the whole position"
        )
        assert sum(ea.calls[0]["pcts"]) < 1.0, (
            f"nothing was reserved for the open runner: {ea.calls[0]['pcts']}"
        )


class TestTheSize:
    @pytest.mark.asyncio
    async def test_risk_pct_from_the_template_is_used_when_set(self, fresh_db):
        calls: list[dict] = []
        await _place(fresh_db, _template(risk_pct=1.5), lot_calls=calls)

        assert calls, "lot sizing was never asked"
        assert calls[-1]["risk_pct"] == pytest.approx(1.5), (
            "sized on the global risk_per_trade_pct instead of the template's risk_pct"
        )

    @pytest.mark.asyncio
    async def test_it_sizes_from_the_resting_price_and_the_templates_stop(self, fresh_db):
        calls: list[dict] = []
        await _place(fresh_db, _template(risk_pct=1.5), lot_calls=calls)

        assert calls[-1]["entry"] == pytest.approx(RESTING_PRICE)
        assert calls[-1]["sl"] == pytest.approx(EXPECTED_SL), (
            "the lot was sized against a stop the order will not use"
        )

    @pytest.mark.asyncio
    async def test_lot_anchor_is_used_when_risk_pct_is_off(self, fresh_db):
        ea = await _place(fresh_db, _template(risk_pct=0.0, lot_anchor=0.02))

        assert ea.calls[0]["lot_size"] == pytest.approx(0.02)

    @pytest.mark.asyncio
    async def test_lot_anchor_is_capped_at_max_lot_size(self, fresh_db):
        """The same cap `scan_auto_execute.py` applies. A template edited to a
        large anchor must not walk past the account's own ceiling."""
        ea = await _place(
            fresh_db, _template(risk_pct=0.0, lot_anchor=5.0),
            rs={"risk_per_trade_pct": 0.5, "strategy_lot_size": 0,
                "max_lot_size": 0.10, "lk_entry_realignment": 0},
        )

        assert ea.calls[0]["lot_size"] == pytest.approx(0.10)


class TestWhoManagesTheFill:
    @pytest.mark.asyncio
    async def test_the_order_is_stamped_with_the_template_strategy(self, fresh_db):
        """`place_pending_order`'s `strategy` is stored on
        `vantage_pending_orders` and read back at fill time to decide how the
        position is managed. A wrong value here mismanages it silently."""
        ea = await _place(fresh_db, _template())

        assert ea.calls[0]["strategy"] == f"template:{TEMPLATE_NAME}", (
            "the fill would be managed as Limit Runner, not by the template"
        )

    @pytest.mark.asyncio
    async def test_the_template_itself_goes_on_the_wire(self, fresh_db):
        """030 is what makes the EA read these, but the Python side must send
        them — a template resolved and then dropped at the boundary is the
        same bug one layer down."""
        ea = await _place(fresh_db, _template())

        assert ea.calls[0]["template"] is not None, (
            "the template was resolved and then dropped at the bridge"
        )
        assert ea.calls[0]["template"]["sl_pips"] == pytest.approx(SL_PIPS)


class TestTheControls:
    """Without these, "always use the template" passes everything above."""

    @pytest.mark.asyncio
    async def test_a_channel_with_no_template_is_unchanged(self, fresh_db):
        ea = await _place(fresh_db, template=None)

        assert ea.calls[0]["stop_loss"] == pytest.approx(4403.0), (
            "an untemplated channel's signal lost its own stop"
        )
        assert ea.calls[0]["tps"] == pytest.approx({1: 4418.0, 2: 4422.0, 3: 4427.0})
        assert ea.calls[0]["template"] is None

    @pytest.mark.asyncio
    async def test_the_resting_price_is_still_the_near_edge(self, fresh_db):
        """Nothing about template resolution may move where the order rests."""
        ea = await _place(fresh_db, _template())

        assert ea.calls[0]["price"] == pytest.approx(RESTING_PRICE)
