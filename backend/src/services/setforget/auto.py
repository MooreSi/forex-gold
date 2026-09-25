"""Set & Forget Auto -- the one unattended path in this domain (2026-09-24).

The owner asked for an "Auto" button: every 15 minutes review the market with
the configured AI, look for a LONG setup, and execute it if there is one. His
choices that day: buy at any validated demand zone whatever the
higher-timeframe bias, with the AI deciding; at most two Auto trades a day;
one Auto position open at a time.

Everything else on the Set & Forget page waits for a person. This does not,
so most of this file is refusals, checked cheapest first:

1. Auto is off -> nothing is read and nothing is billed.
2. The account is not DEMO -> refused. Live needs the owner's sign-off and a
   demo session first (CLAUDE.md, docs/system/rules/20-trading-safety.md).
3. Two Auto trades already today, or one still open -> refused.
4. No AI configured -> refused, because the AI is what decides.
5. The rules produce no long, or it has not TRIGGERED on the 30m, or it breaks
   a rule (`setup.invalidations`) -> no trade, with the rules' own reason.
6. The AI does not say "take" ("adjust" counts when its levels pass the rules
   again -- `analysis.review` rebuilds and re-validates them).
7. The stop would risk more than `MAX_RISK_PCT` of the balance at the lot
   that would be sent -> refused. A $900 demo account at the 0.01-lot floor
   cannot size a wide stop down to its risk setting.

The order goes through `engine.open_manual_market_order` -- the same call the
page's Execute button reaches through `/orders/market` -- tagged with
`SOURCE_NAME` so the ledger can count it. There is no second order path.

**Measured before it was built** (replay of the rules over 330 days of gold,
docs/system/domains/trading/020-set-and-forget.md): longs at any demand zone
triggered about twice a month, and without an AI filter 3 of 22 won, -6.7R.
It is on demo to find out whether the AI's judgement changes that.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backend.src.db import database as _config
from backend.src.services.ai import provider as _ai
from backend.src.services.risk import settings as _risk
from backend.src.services.setforget import analysis as _analysis
from backend.src.services.setforget import auto_repo as _repo
from backend.src.services.setforget import setup as _setup

log = logging.getLogger(__name__)

ENABLED_KEY = "setforget_auto_enabled"
INTERVAL_S = 15 * 60
MAX_TRADES_PER_DAY = 2
MAX_OPEN = 1
# The most of the balance one Auto trade may put at risk, at the lot actually
# sent. Above the page's own risk setting on purpose: the 0.01-lot floor means
# a small account cannot size every stop down to it. A ceiling, not a target.
MAX_RISK_PCT = 5.0
DIRECTION = "BUY"
STRATEGY = "set_and_forget"
SOURCE_NAME = "Set & Forget Auto"
_MIN_LOT = 0.01

# What the page shows. Process state on purpose: it describes this run.
_status: dict = {"last_run": None, "next_run": None, "decision": None,
                 "reason": "", "trade": None, "ai": None}


def is_enabled() -> bool:
    return (_config.get_app_config(ENABLED_KEY) or "") == "1"


def set_enabled(on: bool) -> None:
    _config.set_app_config(ENABLED_KEY, "1" if on else "0")
    log.info("[setforget-auto] switched %s", "ON" if on else "OFF")


def status() -> dict:
    """Whether Auto is on, and what its last scan decided."""
    return {**_status, "enabled": is_enabled(),
            "interval_s": INTERVAL_S, "max_trades_per_day": MAX_TRADES_PER_DAY,
            "source_name": SOURCE_NAME}


def _day_start(now: float) -> float:
    d = datetime.fromtimestamp(now, timezone.utc)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _record(decision: str, reason: str, now: float, **extra) -> dict:
    _status.update(last_run=now, decision=decision, reason=reason,
                   trade=extra.get("trade"), ai=extra.get("ai"))
    log.info("[setforget-auto] %s: %s", decision, reason)
    return {"decision": decision, "reason": reason, **extra}


async def _balance(engine: Any) -> Optional[float]:
    try:
        account = await engine.get_mt5_account()
    except Exception as exc:
        log.warning("[setforget-auto] could not read the account: %s", exc)
        return None
    balance = float((account or {}).get("balance") or 0.0)
    return balance if balance > 0 else None


def _lot(candidate: dict, balance: float, risk: dict) -> float:
    """The page's lot when one is set, else the app's shared risk sizer."""
    fixed = float(risk.get("setforget_lot_size") or 0.0)
    if fixed > 0:
        return round(fixed, 2)
    lot = _setup.lot_from_risk(candidate["entry"], candidate["stop_loss"],
                               balance, float(risk.get("risk_per_trade_pct") or 0.5))
    return max(_MIN_LOT, round(float(lot or _MIN_LOT), 2))


async def tick(engine: Any, cfg: dict, now: Optional[float] = None) -> dict:
    """One scan. Returns what it decided and why; never raises."""
    now = time.time() if now is None else now
    if not is_enabled():
        return _record("off", "Auto is off.", now)

    if str(cfg.get("account_env") or "").lower() != "demo":
        return _record("refused", "Auto trades on the demo account only. "
                       "Trading it live needs the owner's sign-off after a "
                       "demo session.", now)

    done = _repo.trades_today(SOURCE_NAME, _day_start(now))
    if done >= MAX_TRADES_PER_DAY:
        return _record("refused", f"{done} Auto trades already today; the cap "
                       f"is {MAX_TRADES_PER_DAY}.", now)
    if _repo.open_positions(SOURCE_NAME) >= MAX_OPEN:
        return _record("refused", "An Auto trade is still open. One at a time.",
                       now)
    if not _ai.is_configured(cfg):
        return _record("refused", "No AI provider is configured, and Auto "
                       "trades only what the AI approves.", now)

    try:
        evidence = await _analysis.gather(engine)
    except Exception as exc:
        return _record("failed", f"Could not read the chart: {exc}", now)

    candidate, why = _analysis.propose(evidence, direction=DIRECTION)
    if candidate is None:
        return _record("no_setup", why, now)
    broken = _setup.invalidations(candidate)
    if broken:
        return _record("no_setup", broken[0], now, trade=candidate)

    reviewed = await _analysis.review(evidence, candidate, cfg)
    ai = reviewed.get("ai") or {}
    verdict = ai.get("verdict")
    if ai.get("error") or verdict not in ("take", "adjust"):
        reason = ai.get("error") or f"The AI said {verdict or 'nothing'}: " \
                                    f"{ai.get('reasoning') or ''}".strip()
        return _record("ai_declined", reason, now, trade=candidate, ai=ai)

    chosen = reviewed.get("candidate") or candidate
    if chosen.get("order_type", "market") != "market":
        return _record("ai_declined", f"The AI moved the entry to "
                       f"{chosen['entry']:.2f}, away from price. Auto buys at "
                       f"the market, so those levels do not describe the "
                       f"trade it would place.", now, trade=chosen, ai=ai)
    broken = _setup.invalidations(chosen)
    if broken:
        return _record("no_setup", broken[0], now, trade=chosen, ai=ai)

    balance = await _balance(engine)
    if balance is None:
        return _record("failed", "Could not read the account balance.", now)
    risk = _risk.get() or {}
    lot = _lot(chosen, balance, risk)
    at_risk = _setup.money_at_risk(chosen, lot) or 0.0
    if at_risk > balance * MAX_RISK_PCT / 100.0:
        return _record("refused", f"This stop risks ${at_risk:.2f} at {lot} "
                       f"lots, {at_risk / balance * 100:.1f}% of the balance; "
                       f"Auto's ceiling is {MAX_RISK_PCT:g}%.", now,
                       trade=chosen, ai=ai)

    try:
        placed = await engine.open_manual_market_order(
            direction=DIRECTION, stop_loss=chosen["stop_loss"], lot_size=lot,
            strategy=STRATEGY, take_profit=chosen["take_profit"],
            source_name=SOURCE_NAME,
        )
    except Exception as exc:
        return _record("failed", f"The order was not placed: {exc}", now,
                       trade=chosen, ai=ai)
    return _record("placed", f"Bought {lot} lots, stop {chosen['stop_loss']:.2f}, "
                   f"target {chosen['take_profit']:.2f}.", now,
                   trade={**chosen, "lot": lot, "order": placed}, ai=ai)


# How often the loop wakes to ask whether a scan is due. Short, so switching
# Auto on scans within seconds instead of after a full interval.
_WAKE_S = 30.0


def scan_due(now: float, last_scan: Optional[float]) -> bool:
    """A scan is due on the first pass after Auto is switched on, then every
    `INTERVAL_S`."""
    return last_scan is None or now - last_scan >= INTERVAL_S


async def run_forever(get_engine: Callable[[], Any],
                      load_cfg: Callable[[], dict]) -> None:
    """Scan every `INTERVAL_S` while Auto is on. Started once from
    `app.startup`; never returns and never lets one scan's failure end it."""
    last_scan: Optional[float] = None
    while True:
        now = time.time()
        if not is_enabled():
            last_scan = None
            _status["next_run"] = None
        elif scan_due(now, last_scan):
            last_scan = now
            _status["next_run"] = now + INTERVAL_S
            try:
                await tick(get_engine(), load_cfg(), now=now)
            except Exception as exc:          # never let the loop die
                log.exception("[setforget-auto] scan failed: %s", exc)
                _record("failed", f"Scan failed: {exc}", now)
        await asyncio.sleep(_WAKE_S)
