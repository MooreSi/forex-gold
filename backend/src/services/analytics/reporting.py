"""Read-only trade reporting -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine.get_open_trades/get_all_trades/
compute_performance, as part of the core/engine.py migration series. See
docs/todo/refactor/core-trade-reporting-migration/020-*.md.

get_open_trades/get_all_trades never used `self`. compute_performance read
self._cfg.get("starting_balance", ...) -- it now takes starting_balance as
an explicit parameter instead (same pattern as core_sim_account.py's
reset_simulation).
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime
from typing import Optional

from backend.src.db import database as db_module

# How long a $0-entry/ticket-0 EA Template placeholder row is allowed to look
# like a normal in-flight fill before display code should stop rendering it
# as an Active Trade card. Well past the EA's own ack-timeout ceiling (60s,
# core_open_trade._ack_timeout) and the grid-leg expiry window, so this never
# hides a trade that is still genuinely being placed -- only ones that are
# stuck (see core_template_placeholder_repair.py for why a row can get stuck
# despite the self-healers, and core_open_trade.py's legs_placed==0 guard for
# the specific bug that used to make a grid-total-failure row unrecoverable).
_STUCK_PLACEHOLDER_GRACE_S = 180


def is_stuck_placeholder(trade: dict) -> bool:
    """True for an open row that never got a real ticket/entry and has been
    open long enough that it can no longer be a normal in-flight fill.

    Display-only signal -- never used to gate trading/risk logic. Real
    open-trade counts, duplicate checks, TP Safety Net etc. all still need to
    see these rows (they may yet resolve, and hiding them there could mask a
    genuine broker position); only the Active Trades UI/chart panel should
    stop rendering one as an ordinary card once it's stale."""
    if (trade.get("mt5_ticket") or 0) or float(trade.get("entry_price") or 0):
        return False
    open_time = float(trade.get("open_time") or 0)
    return open_time > 0 and (time.time() - open_time) > _STUCK_PLACEHOLDER_GRACE_S


def get_open_trades() -> list[dict]:
    with db_module.db() as conn:
        rows = conn.execute(
            "SELECT t.*, s.source_name AS _sig_source "
            "FROM vantage_simulated_trades t "
            "LEFT JOIN vantage_signals s ON s.signal_id = t.signal_id "
            "WHERE t.status='open' ORDER BY t.open_time DESC"
        ).fetchall()
    result = [db_module.row_to_dict(r) for r in rows]
    for t in result:
        if not t.get("tg_source") and t.get("_sig_source"):
            t["tg_source"] = t["_sig_source"]
        t.pop("_sig_source", None)
        for key in ("claude_open", "claude_close"):
            if t.get(key):
                try:
                    t[key] = json.loads(t[key])
                except Exception:
                    pass
    return result


def get_all_trades(status: Optional[str] = None, limit: int = 100) -> list[dict]:
    with db_module.db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM vantage_simulated_trades WHERE status=?"
                " ORDER BY open_time DESC LIMIT ?", (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM vantage_simulated_trades ORDER BY open_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
    result = [db_module.row_to_dict(r) for r in rows]
    for t in result:
        for key in ("claude_open", "claude_close"):
            if t.get(key):
                try:
                    t[key] = json.loads(t[key])
                except Exception:
                    pass
    return result


def compute_performance(starting_balance: float) -> dict:
    starting = float(starting_balance)
    with db_module.db() as conn:
        acc    = db_module.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulation_account WHERE id=1").fetchone()
        )
        closed = [db_module.row_to_dict(r) for r in conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE status='closed'"
            " ORDER BY close_time ASC"
        ).fetchall()]
        open_t = [db_module.row_to_dict(r) for r in conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE status='open'"
        ).fetchall()]

    balance   = float(acc.get("balance", starting))
    net_pnls  = [float(t.get("net_pnl", 0)) for t in closed]
    winners   = [p for p in net_pnls if p > 0]
    losers    = [p for p in net_pnls if p < 0]
    win_rate  = len(winners) / len(net_pnls) * 100 if net_pnls else 0.0
    avg_win   = sum(winners) / len(winners) if winners else 0.0
    avg_loss  = sum(losers) / len(losers) if losers else 0.0
    pf        = (sum(winners) / abs(sum(losers))) if losers and sum(winners) > 0 else 0.0

    hold_times = []
    for t in closed:
        if t.get("open_time") and t.get("close_time"):
            hold_times.append(float(t["close_time"]) - float(t["open_time"]))
    avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0.0

    cumulative = starting
    peak   = starting if starting > 0 else 1.0
    trough = cumulative
    max_dd = 0.0
    max_ru = 0.0
    for p in net_pnls:
        cumulative += p
        if cumulative < trough:
            trough = cumulative
        if cumulative > peak:
            peak = cumulative
        if peak > 0:
            dd = (peak - cumulative) / peak * 100
            if dd > max_dd:
                max_dd = dd
        if trough > 0:
            ru = (cumulative - trough) / trough * 100
            if ru > max_ru:
                max_ru = ru

    # Local calendar-day cutoff for sim mode (close_time is proper UTC epoch)
    _uk_day_cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    _daily_closed = [t for t in closed
                     if float(t.get("close_time", 0)) >= _uk_day_cutoff]
    _daily_pnls   = [float(t.get("net_pnl", 0)) for t in _daily_closed]
    daily_pnl     = sum(_daily_pnls)
    _daily_wins   = [p for p in _daily_pnls if p > 0]
    _daily_wr     = (
        round(len(_daily_wins) / len(_daily_pnls) * 100, 1)
        if _daily_pnls else 0.0
    )

    # Sharpe and Sortino ratios (trade P&L as returns; no risk-free rate subtracted)
    if len(net_pnls) > 1:
        _mean_r = sum(net_pnls) / len(net_pnls)
        _std_r  = math.sqrt(sum((r - _mean_r) ** 2 for r in net_pnls) / (len(net_pnls) - 1))
        sharpe  = round(_mean_r / _std_r, 3) if _std_r > 0 else 0.0
        _neg    = [r for r in net_pnls if r < 0]
        if _neg:
            _down_std = math.sqrt(sum(r ** 2 for r in _neg) / len(net_pnls))
            sortino   = round(_mean_r / _down_std, 3) if _down_std > 0 else 0.0
        else:
            sortino = 0.0
    else:
        sharpe = sortino = 0.0

    # Equity high-watermark from app_config (updated on each close)
    _peak_bal = float(db_module.get_app_config("peak_balance") or balance)

    return {
        "starting_balance":   starting,
        "current_balance":    round(balance, 4),
        "equity":             round(balance, 4),
        "peak_balance":       round(_peak_bal, 4),
        "total_net_pnl":      round(sum(net_pnls), 4),
        "open_trades":        len(open_t),
        "closed_trades":      len(closed),
        "win_rate_pct":       round(win_rate, 2),
        "loss_rate_pct":      round(100 - win_rate, 2),
        "avg_win":            round(avg_win, 4),
        "avg_loss":           round(avg_loss, 4),
        "profit_factor":      round(pf, 4),
        "sharpe_ratio":       sharpe,
        "sortino_ratio":      sortino,
        "best_trade":         round(max(net_pnls), 4) if net_pnls else 0.0,
        "worst_trade":        round(min(net_pnls), 4) if net_pnls else 0.0,
        "avg_hold_seconds":   round(avg_hold, 1),
        "max_drawdown_pct":   round(max_dd, 2),
        "max_runup_pct":      round(max_ru, 2),
        # UK calendar-day stats
        "daily_pnl_24h":      round(daily_pnl, 4),  # kept for email compat
        "daily_pnl":          round(daily_pnl, 4),
        "daily_closed":       len(_daily_pnls),
        "daily_win_rate_pct": _daily_wr,
        "daily_best":         round(max(_daily_pnls), 2) if _daily_pnls else 0.0,
        "daily_worst":        round(min(_daily_pnls), 2) if _daily_pnls else 0.0,
    }


# ── Read-side wrappers for the UI ────────────────────────────────────────────
# The AI summary and the Heat Map used to import these repos directly from the
# page. That broke the frontend-through-controllers contract, and routing them
# straight into a controller only moved the break, because controllers may not
# import a repo either. They belong on a service, which is this one.

def recent_tg_signals(*args, **kwargs):
    """Recent parsed Telegram signals, for the AI summary page."""
    from backend.src.services.analytics import read_repo as _read_repo
    return _read_repo.recent_tg_signals(*args, **kwargs)


def signal_lab_is_available() -> bool:
    """Whether the signal-lab tables exist. The Heat Map hides itself when
    they do not, rather than erroring on a fresh install."""
    from backend.src.services.analytics import signal_lab_repo as _lab
    return _lab.is_available()


def signal_lab_adx_and_bias_samples(*args, **kwargs):
    from backend.src.services.analytics import signal_lab_repo as _lab
    return _lab.adx_and_bias_samples(*args, **kwargs)
