"""EA Template TP resolution -- which levels a template trade actually opens
with, and what the trade row records.

Found live 2026-07-30: a Sig Gen Grid trade on Reversal Engine alerted 8 TP
levels (4040.68/4041.68/...) that were the Reversal Engine SIGNAL's own,
while the EA was running the template's 6 (20/40/60/80/100/120 pips). The EA
handoff resolved the template correctly; the INSERT wrote the signal's
arguments. Everything reading the row -- the Telegram alert, the UI, TP
Safety Net -- therefore reported levels nobody was trading.

Also covers "Use TP Levels from Telegram", the per-ladder switch that makes a
Telegram message's own TP prices win over the pips column.

tp{n}_pips (and the pips resolve_template_tps computes for the pending
ladder) is genuine pips as of 2026-07-31 -- 1 pip = 0.10 price on this
XAUUSD feed, matching the reference channels' own wording and
ForexTraderBridge.mq5's PipsToPrice(). Before that fix these were added to
price as raw points, placing every anchor TP 10x further out than a pips
value implies. See core_pips.PIPS_TO_PRICE_XAUUSD.
"""
import os
import tempfile

import pytest

from backend.src.services.broker import ea_templates as et
from backend.src.db import database as db
from backend.src.services.trading.open_trade import is_telegram_source, resolve_template_tps


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db.save_channel_parser_config("Gold Diggers VIP", "standard", "", False, True, "")
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _Tick:
    """Spread deliberately non-zero: the EA stages a grid from the opposite
    side of the book to Python's reference price, so any conversion that
    picks the wrong side lands a full spread out."""
    ask = 4039.18
    bid = 4038.94


# Sig Gen Grid as configured live: 6 pip levels, only 4 with a close %.
SIG_GEN_GRID = {
    "mode": "grid", "anchors": 1, "pendings": 3,
    **{f"tp{n}_pips": p for n, p in zip(range(1, 7), [20, 40, 60, 80, 100, 120])},
    **{f"tp{n}_pct": p for n, p in zip(range(1, 5), [10, 20, 40, 10])},
    **{f"tp_pen{n}_pips": p for n, p in zip(range(1, 7), [20, 40, 60, 80, 100, 120])},
}

# The Reversal Engine signal from the reported trade.
SIGNAL_TPS = [4040.68, 4041.68, 4042.68, 4043.68, 4045.68, 4047.68, 4052.68, 4067.68]


def _template(fresh_db, **overrides):
    return et.save_ea_template("Sig Gen Grid", {**SIG_GEN_GRID, **overrides})


# ── Source classification ─────────────────────────────────────────────────────

def test_configured_telegram_channel_is_a_telegram_source(fresh_db):
    assert is_telegram_source("Gold Diggers VIP") is True


def test_telegram_auto_variant_is_unwrapped(fresh_db):
    """Executed rows carry the 'Telegram Auto (<name>)' spelling, so the raw
    name alone would classify most real trades as non-Telegram."""
    assert is_telegram_source("Telegram Auto (Gold Diggers VIP)") is True


def test_internal_generators_are_not_telegram_sources(fresh_db):
    for source in ("Reversal Engine", "Breakout Engine", "Bounce Engine",
                   "ORB/IVB Report", "manual_market", None, ""):
        assert is_telegram_source(source) is False, source


# ── Template pips (the default, both switches off) ────────────────────────────

def test_template_pips_define_the_ladder_not_the_signal(fresh_db):
    """The regression: 6 configured pip levels must produce 6 levels, derived
    from the template, with the signal's 8 levels ignored entirely."""
    tps, pcts, pen = resolve_template_tps(
        _template(fresh_db), "BUY", _Tick(), SIGNAL_TPS, "Reversal Engine")
    assert sorted(tps) == [1, 2, 3, 4, 5, 6]
    assert tps[1] == pytest.approx(4041.18)     # ask 4039.18 + 20 pips (2.0 price)
    assert tps[6] == pytest.approx(4051.18)     # ask 4039.18 + 120 pips (12.0 price)
    for level in tps.values():
        assert level not in SIGNAL_TPS
    assert pen is None


def test_levels_past_the_percentage_table_keep_zero_percent(fresh_db):
    """TP5/TP6 are deliberately 0% on this template -- the trailing stop takes
    anything past TP4. A ladder-fill that forced 100% onto the last level
    would close the runner the trail is there to hold.

    pcts is a 0-1 fraction on the wire (DoPartialClose: lots = orig_lots *
    pct), not the 0-100 number the % column stores -- 10.0 here means 10%."""
    _, pcts, _ = resolve_template_tps(
        _template(fresh_db), "BUY", _Tick(), SIGNAL_TPS, "Reversal Engine")
    assert pcts == pytest.approx([0.10, 0.20, 0.40, 0.10, 0.0, 0.0])


def test_sell_direction_subtracts_pips(fresh_db):
    tps, _, _ = resolve_template_tps(
        _template(fresh_db), "SELL", _Tick(), SIGNAL_TPS, "Reversal Engine")
    assert tps[1] == pytest.approx(4036.94)     # bid 4038.94 - 20 pips (2.0 price)


def test_template_with_no_pips_sends_no_levels(fresh_db):
    blank = et.save_ea_template("Blank", {"mode": "grid"})
    tps, pcts, _ = resolve_template_tps(
        blank, "BUY", _Tick(), SIGNAL_TPS, "Reversal Engine")
    assert tps == {}
    assert pcts is None


# ── Use TP Levels from Telegram: anchor ───────────────────────────────────────

def test_telegram_levels_win_when_the_switch_is_on(fresh_db):
    tpl = _template(fresh_db, tp_from_telegram=True)
    tps, _, _ = resolve_template_tps(
        tpl, "BUY", _Tick(), SIGNAL_TPS, "Gold Diggers VIP")
    assert [tps[n] for n in sorted(tps)] == SIGNAL_TPS


def test_message_sets_the_level_count(fresh_db):
    tpl = _template(fresh_db, tp_from_telegram=True)
    tps, _, _ = resolve_template_tps(
        tpl, "BUY", _Tick(), [4040.68, 4041.68, 4042.68], "Gold Diggers VIP")
    assert sorted(tps) == [1, 2, 3]


def test_last_telegram_level_closes_the_remainder(fresh_db):
    """A message with more levels than the % table defines must not leave an
    unclosable tail."""
    tpl = _template(fresh_db, tp_from_telegram=True)
    _, pcts, _ = resolve_template_tps(
        tpl, "BUY", _Tick(), SIGNAL_TPS, "Gold Diggers VIP")
    assert pcts[-1] == 1.0
    assert pcts[:4] == pytest.approx([0.10, 0.20, 0.40, 0.10])


def test_switch_does_not_apply_to_internal_generators(fresh_db):
    """The switch is Telegram-only by design: an internal generator has no
    message, so its trades keep using the editable pips column."""
    tpl = _template(fresh_db, tp_from_telegram=True)
    tps, _, _ = resolve_template_tps(
        tpl, "BUY", _Tick(), SIGNAL_TPS, "Reversal Engine")
    assert tps[1] == pytest.approx(4041.18)     # 20 pips = 2.0 price
    assert sorted(tps) == [1, 2, 3, 4, 5, 6]


def test_telegram_signal_with_no_levels_falls_back_to_the_pips(fresh_db):
    tpl = _template(fresh_db, tp_from_telegram=True)
    tps, _, _ = resolve_template_tps(
        tpl, "BUY", _Tick(), [None] * 8, "Gold Diggers VIP")
    assert tps[1] == pytest.approx(4041.18)     # 20 pips = 2.0 price


# ── Use TP Levels from Telegram: pending ──────────────────────────────────────

def test_pending_untouched_when_its_switch_is_off(fresh_db):
    tpl = _template(fresh_db, tp_from_telegram=True)
    _, _, pen = resolve_template_tps(
        tpl, "BUY", _Tick(), SIGNAL_TPS, "Gold Diggers VIP")
    assert pen is None


def test_both_switches_on_puts_pending_on_the_absolute_levels(fresh_db):
    """With the anchor already carrying the message's prices on the wire, the
    pending ladder is switched to the EA's absolute-tp fallback by zeroing
    its pips (see HandleOpenTemplateGrid)."""
    tpl = _template(fresh_db, tp_from_telegram=True, tp_pen_from_telegram=True)
    _, _, pen = resolve_template_tps(
        tpl, "BUY", _Tick(), SIGNAL_TPS, "Gold Diggers VIP")
    assert pen == {}


def test_pending_only_converts_message_prices_to_pips_from_the_ea_base(fresh_db):
    """Anchor stays on the template while pending follows the message, so the
    absolute tp{n} on the wire belong to the anchor and the fallback would
    hand pending the wrong ladder. Converted to genuine pips from the EA's
    OWN staging base (bid for a BUY) -- the EA applies its own PipsToPrice()
    (pips * 10 * _Point) to whatever arrives here, so this must send pips,
    not the raw price gap, or the pending leg's TP lands 10x closer than the
    message actually stated."""
    tpl = _template(fresh_db, tp_pen_from_telegram=True)
    _, _, pen = resolve_template_tps(
        tpl, "BUY", _Tick(), SIGNAL_TPS, "Gold Diggers VIP")
    assert pen[1] == pytest.approx((4040.68 - _Tick.bid) / 0.10)
    # and the EA's own arithmetic (PipsToPrice) gets back to the message's level
    assert _Tick.bid + pen[1] * 0.10 == pytest.approx(4040.68)


def test_pending_only_uses_the_ask_side_for_a_sell(fresh_db):
    tpl = _template(fresh_db, tp_pen_from_telegram=True)
    _, _, pen = resolve_template_tps(
        tpl, "SELL", _Tick(), [4030.0], "Gold Diggers VIP")
    assert _Tick.ask - pen[1] * 0.10 == pytest.approx(4030.0)


def test_pending_switch_ignored_for_internal_generators(fresh_db):
    tpl = _template(fresh_db, tp_pen_from_telegram=True)
    _, _, pen = resolve_template_tps(
        tpl, "BUY", _Tick(), SIGNAL_TPS, "Reversal Engine")
    assert pen is None


# ── Defaults ──────────────────────────────────────────────────────────────────

def test_new_templates_default_to_the_pips_column(fresh_db):
    """Existing templates must keep behaving exactly as before this feature."""
    tpl = et.save_ea_template("Default", {})
    assert tpl["tp_from_telegram"] is False
    assert tpl["tp_pen_from_telegram"] is False


# ── What the trade row records ────────────────────────────────────────────────

class _FakeEA:
    """A healthy EA that accepts the order, so open_trade takes the template
    handoff path and reaches its INSERT."""

    def __init__(self):
        self.sent = {}

    def is_ea_healthy(self):
        return True

    def is_strategy_portable(self, strategy):
        return True

    async def open_trade(self, trade_id, direction, lot, sl, tps, strategy, **kw):
        self.sent = {"tps": dict(tps), "kwargs": kw}
        return {"type": "trade_opened", "ticket": 0, "fill_price": 0.0}


class _FakeBridge:
    async def get_fresh_tick(self):
        return _Tick()


def _open_template_trade(monkeypatch, fresh_db, tg_source, template_overrides=None):
    import asyncio

    from backend.src.services.trading import open_trade as cot
    from backend.src.services.broker import ea_bridge as ea_mod

    import time

    _template(fresh_db, **(template_overrides or {}))
    db.update_risk_settings({"ea_bridge_enabled": 1, "max_open_trades": 10})
    # vantage_simulated_trades.signal_id is a foreign key.
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("sig-1", tg_source or "test", "BUY", 4036.18, 4039.18, 4033.68,
             "new", time.time()),
        )

    ea = _FakeEA()
    monkeypatch.setattr(ea_mod, "get_instance", lambda: ea)

    result = asyncio.run(cot.open_trade(
        _FakeBridge(), "sig-1", "BUY", 4036.18, 4039.18, 4033.68,
        *SIGNAL_TPS,
        lot_size=0.04, tick=_Tick(),
        strategy=et.override_for_template("Sig Gen Grid"),
        tg_source=tg_source,
    ))
    with db.db() as conn:
        row = db.row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id=?",
            (result["trade_id"],),
        ).fetchone())
    return row, ea


def test_row_records_the_templates_levels_not_the_signals(monkeypatch, fresh_db):
    """The reported bug, end to end: the row must describe what the EA is
    running. Six configured pip levels means six levels in the row and
    NULL past that -- not the signal's eight."""
    row, ea = _open_template_trade(monkeypatch, fresh_db, "Reversal Engine")

    assert row["tp1"] == pytest.approx(4041.18)     # 20 pips = 2.0 price
    assert row["tp6"] == pytest.approx(4051.18)     # 120 pips = 12.0 price
    assert row["tp7"] is None and row["tp8"] is None
    for n in range(1, 9):
        assert row[f"tp{n}"] not in SIGNAL_TPS


def test_row_matches_exactly_what_was_sent_to_the_ea(monkeypatch, fresh_db):
    row, ea = _open_template_trade(monkeypatch, fresh_db, "Reversal Engine")
    for n, level in ea.sent["tps"].items():
        assert row[f"tp{n}"] == pytest.approx(level)


def test_row_records_telegram_levels_when_the_switch_is_on(monkeypatch, fresh_db):
    row, ea = _open_template_trade(
        monkeypatch, fresh_db, "Gold Diggers VIP", {"tp_from_telegram": True})
    assert [row[f"tp{n}"] for n in range(1, 9)] == SIGNAL_TPS


def test_pending_pips_are_rewritten_only_on_the_copy_sent_to_the_ea(
        monkeypatch, fresh_db):
    """The stored template must keep the user's pending pips -- they are still
    what internal-generator trades use."""
    _, ea = _open_template_trade(
        monkeypatch, fresh_db, "Gold Diggers VIP",
        {"tp_from_telegram": True, "tp_pen_from_telegram": True})
    assert ea.sent["kwargs"]["template"]["tp_pen1_pips"] == 0.0
    assert et.get_ea_template("Sig Gen Grid")["tp_pen1_pips"] == 20.0
