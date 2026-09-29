"""Trend PA's one real-money path. Called only when `tpa_live_execution` is on.

Hands a virtual signal to the main engine as source "Trend PA Engine", the
same way Breakout does: `create_signal` then `open_trade_from_signal`. So
every account-level gate -- the trading pause (daily loss, give-back, daily
goal), the circuit breaker, max open trades, the Local/Remote forwarding --
applies exactly as it does to every other source, in `open_trade`.

What this adds before that, each a refusal recorded on the signal:

  * a strategy must have been CHOSEN for "Trend PA Engine" (Trading >
    Strategy, or a schedule window's override). The global default is not
    assumed: several strategies replace a signal's own stop and target, and
    this engine's 1:2 is the whole strategy.
  * the Trading Schedule, under the engine's own per-window toggle.
  * an ARMED model must expect a positive R. An unarmed one has no say.

Size is left to the account's risk settings (`lot_size=None`).

**Not yet run against a demo account.** docs/todo/012 says what that
session must watch before this goes near the live account.
"""
from __future__ import annotations

import logging

from backend.src.services.trend_pa import ml
from backend.src.services.trend_pa import repo
from backend.src.services.trend_pa import strategy as st

log = logging.getLogger("trend_pa")

SOURCE_NAME = "Trend PA Engine"
# vantage_signals wants an entry range, not a price. A market entry, so a
# narrow one around the fill.
_ZONE_HALF_WIDTH = 0.5


def _strategy():
    from backend.src.services.risk.schedule import effective_channel_strategy
    return effective_channel_strategy(SOURCE_NAME)


def _schedule() -> tuple:
    """Under the engine's own window key, `trend_pa_engine`, so each window's
    Trend PA toggle applies -- not the Telegram default."""
    from backend.src.services.risk import schedule as sched
    return sched.check_trading_schedule(source=sched.schedule_source_key(SOURCE_NAME))


async def execute(engine, sig: dict, tick) -> None:
    sid, ref = sig["id"], sig.get("signal_ref") or f"TPA-{sig['id']:04d}"
    main = engine._main_engine
    if main is None:
        repo.update_live_exec(sid, "skipped:no_main_engine")
        return
    if not _strategy():
        log.info("[TPA-Live] %s skipped -- no strategy chosen for %s", ref, SOURCE_NAME)
        repo.update_live_exec(sid, "skipped:no_strategy")
        return
    ok, why = _schedule()
    if not ok:
        repo.update_live_exec(sid, f"skipped:schedule:{why}"[:200])
        return
    p = engine.model.predict(sig.get("features") or {})
    if not ml.gate_passes(p, rr=st.DEFAULTS["rr"], cost_r=0.0):
        repo.update_live_exec(sid, f"skipped:ML p(win)={p:.2f} expects a loss at 1:{st.DEFAULTS['rr']:g}")
        return

    vsid = None
    try:
        entry = float(sig["entry"])
        made = main.create_signal(
            source_name=SOURCE_NAME, direction=sig["direction"],
            entry_low=round(entry - _ZONE_HALF_WIDTH, 2),
            entry_high=round(entry + _ZONE_HALF_WIDTH, 2),
            stop_loss=float(sig["stop_loss"]), tp1=float(sig["take_profit"]),
            lot_size=None, notes=ref,
        )
        vsid = made["signal_id"]
        result = await main.open_trade_from_signal(vsid, tick=tick)
        repo.update_live_exec(sid, "success", mt5_ticket=result.get("mt5_ticket"),
                              vantage_signal_id=vsid)
        log.info("[TPA-Live] %s opened ticket=%s", ref, result.get("mt5_ticket"))
    except Exception as exc:
        reason = str(exc)[:160]
        log.warning("[TPA-Live] %s failed: %s", ref, reason)
        repo.update_live_exec(sid, f"failed:{reason}", vantage_signal_id=vsid)
