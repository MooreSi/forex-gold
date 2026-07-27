#!/usr/bin/env python3
"""
Signal Generator Daily Report
Sends a Telegram message + HTML email with daily and accumulated stats
from the test signal engine (test_signal.db).

Run standalone:
    cd /Users/simon/Documents/FOREX
    /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 signal_generator_report.py
"""

import asyncio
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
_FOREX_DIR = Path(__file__).parent
sys.path.insert(0, str(_FOREX_DIR))

from backend.src.config import DATA_DIR

_DB_PATH = DATA_DIR / "test_signal.db"

# ── Colour constants (reused in email) ────────────────────────────────────────
_GOLD   = "#f59e0b"
_BLUE   = "#60a5fa"
_GREEN  = "#10b981"
_RED    = "#ef4444"
_BG     = "#1f2937"
_CARD   = "#374151"
_DARK   = "#111827"
_TEXT   = "#e5e7eb"
_SUB    = "#9ca3af"
_BORDER = "#4b5563"


# ── DB helpers ─────────────────────────────────────────────────────────────────

def _query(sql: str, params=()) -> list[dict]:
    with sqlite3.connect(str(_DB_PATH)) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def _scalar(sql: str, params=(), default=None):
    rows = _query(sql, params)
    if rows:
        val = list(rows[0].values())[0]
        return val
    return default


def _get_stats(where: str = "", params=()) -> dict:
    """Return aggregated stats for a subset of test_signals (win/loss only)."""
    base = f"FROM test_signals WHERE outcome IN ('win','loss') {where}"
    rows = _query(f"""
        SELECT
          COUNT(*)                                                         AS total,
          SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END)                AS wins,
          SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END)                AS losses,
          ROUND(SUM(pnl_dollars), 2)                                      AS total_pnl,
          ROUND(AVG(pnl_dollars), 2)                                      AS avg_pnl,
          ROUND(MAX(pnl_dollars), 2)                                      AS best_trade,
          ROUND(MIN(pnl_dollars), 2)                                      AS worst_trade,
          ROUND(SUM(CASE WHEN pnl_dollars>0 THEN pnl_dollars ELSE 0 END),2) AS gross_profit,
          ROUND(ABS(SUM(CASE WHEN pnl_dollars<0 THEN pnl_dollars ELSE 0 END)),2) AS gross_loss,
          ROUND(100.0*SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END)
                / MAX(COUNT(*),1), 1)                                    AS win_rate,
          ROUND(AVG(CASE WHEN claude_approved=1 AND outcome='win' THEN 1.0
                         WHEN claude_approved=1 AND outcome='loss' THEN 0.0 END)*100,0)
                                                                         AS claude_approve_wr,
          ROUND(AVG(CASE WHEN claude_approved=0 AND outcome='win' THEN 1.0
                         WHEN claude_approved=0 AND outcome='loss' THEN 0.0 END)*100,0)
                                                                         AS claude_reject_wr,
          SUM(CASE WHEN claude_approved=1 THEN 1 ELSE 0 END)            AS claude_approved_n,
          SUM(CASE WHEN claude_approved=0 THEN 1 ELSE 0 END)            AS claude_rejected_n,
          ROUND(SUM(CASE WHEN claude_approved=1 THEN pnl_dollars ELSE 0 END),2) AS claude_approved_pnl,
          ROUND(SUM(CASE WHEN claude_approved=0 THEN pnl_dollars ELSE 0 END),2) AS claude_rejected_pnl
        {base}
    """, params)
    return rows[0] if rows else {}


def _get_balance() -> dict:
    rows = _query("SELECT * FROM test_balance_log ORDER BY id DESC LIMIT 1")
    if rows:
        row = rows[0]
        # normalise: column is 'balance' — alias to 'balance_after' for uniform access
        row.setdefault("balance_after", row.get("balance", 1000.0))
        return row
    return {"balance_after": 1000.0, "balance": 1000.0}


def _get_today_signals() -> list[dict]:
    today = date.today().isoformat()
    return _query("""
        SELECT id, direction, outcome, pnl_dollars, pnl_pts, lot_size,
               entry_mid, close_price, key_level_type, session,
               claude_approved, rationale,
               datetime(created_at, 'unixepoch', 'localtime') as created_at_dt,
               datetime(close_time,  'unixepoch', 'localtime') as close_time_dt
        FROM test_signals
        WHERE outcome IN ('win','loss')
          AND date(close_time, 'unixepoch', 'localtime') = ?
        ORDER BY close_time ASC
    """, (today,))


def _get_open_signal() -> dict | None:
    rows = _query("""
        SELECT id, direction, entry_mid, lot_size, created_at,
               datetime(created_at, 'unixepoch', 'localtime') as created_at_dt
        FROM test_signals WHERE outcome = 'open' ORDER BY id DESC LIMIT 1
    """)
    return rows[0] if rows else None


# ── Telegram formatter ─────────────────────────────────────────────────────────

def build_telegram_text(
    daily: dict, accum: dict, balance: dict,
    today_trades: list[dict], open_sig: dict | None,
    report_date: str,
) -> str:
    def pnl_emoji(v):
        if v is None: return "➖"
        return "✅" if float(v) >= 0 else "❌"

    def fmt(v, prefix="$"):
        if v is None: return "—"
        v = float(v)
        sign = "+" if v >= 0 else ""
        return f"{sign}{prefix}{abs(v):,.2f}"

    def pct(v):
        if v is None: return "—"
        return f"{float(v):.1f}%"

    bal      = float(balance.get("balance_after", 1000) or 1000)
    start_bal = 1000.0
    overall_pnl = bal - start_bal
    overall_pct = (overall_pnl / start_bal) * 100

    d_pnl = float(daily.get("total_pnl") or 0)
    d_n   = int(daily.get("total") or 0)
    d_wr  = float(daily.get("win_rate") or 0)

    a_pnl = float(accum.get("total_pnl") or 0)
    a_n   = int(accum.get("total") or 0)
    a_wr  = float(accum.get("win_rate") or 0)
    a_best  = float(accum.get("best_trade") or 0)
    a_worst = float(accum.get("worst_trade") or 0)

    # Profit factor
    gp = float(accum.get("gross_profit") or 0)
    gl = float(accum.get("gross_loss") or 0)
    pf = round(gp / max(gl, 0.01), 2) if gl else "—"

    # Claude filter
    c_approved_n   = int(accum.get("claude_approved_n") or 0)
    c_rejected_n   = int(accum.get("claude_rejected_n") or 0)
    c_approved_pnl = float(accum.get("claude_approved_pnl") or 0)
    c_rejected_pnl = float(accum.get("claude_rejected_pnl") or 0)
    c_app_wr = accum.get("claude_approve_wr")
    c_rej_wr = accum.get("claude_reject_wr")

    lines = [
        f"📊 *Signal Generator Daily Report*",
        f"_{report_date}_",
        "",
        f"💰 *Virtual Balance: ${bal:,.2f}*  ({'+' if overall_pnl>=0 else ''}{overall_pct:.1f}% from $1,000 start)",
        "",
        f"📅 *TODAY  ({d_n} signals)*",
        f"  P&L: {fmt(d_pnl)}  |  Win rate: {pct(d_wr)}",
    ]

    if today_trades:
        for t in today_trades:
            icon = "✅" if t["outcome"] == "win" else "❌"
            dir_arrow = "↑" if t["direction"] == "BUY" else "↓"
            lines.append(
                f"  {icon} SIG-{t['id']:04d} {dir_arrow}{t['direction']}  "
                f"{fmt(t['pnl_dollars'])}  ({t.get('session','')}, "
                f"{t.get('key_level_type','').replace('_',' ')})"
            )
    elif d_n == 0:
        lines.append("  No signals closed today")

    if open_sig:
        lines.append(f"  🔄 1 signal currently open (SIG-{open_sig['id']:04d} {open_sig['direction']} since {open_sig.get('created_at_dt','?')[11:16]})")

    lines += [
        "",
        f"📈 *ACCUMULATED  ({a_n} signals)*",
        f"  Total P&L: {fmt(a_pnl)}",
        f"  Win rate: {pct(a_wr)}  |  Profit factor: {pf}",
        f"  Best: {fmt(a_best)}  |  Worst: {fmt(a_worst)}",
        "",
        f"🤖 *Claude Filter*",
        f"  Approved ({c_approved_n}): {fmt(c_approved_pnl)}  WR: {pct(c_app_wr) if c_app_wr is not None else '—'}",
        f"  Rejected/bootstrap ({c_rejected_n}): {fmt(c_rejected_pnl)}  WR: {pct(c_rej_wr) if c_rej_wr is not None else '—'}",
    ]

    # Verdict on Claude filter
    if c_approved_n >= 3 and c_rejected_n >= 3:
        if float(c_rejected_pnl) > float(c_approved_pnl):
            lines.append("  ⚠️ Filter inversely correlated — rejected signals outperforming approved")
        else:
            lines.append("  ✅ Filter working — approved signals outperforming")

    return "\n".join(lines)


# ── Email HTML builder ─────────────────────────────────────────────────────────

def _stat_cell(label: str, value: str, color: str = _TEXT) -> str:
    return f"""
      <td align="center" style="padding:12px 8px;border-right:1px solid {_BORDER};">
        <p style="margin:0;color:{_SUB};font-size:10px;text-transform:uppercase;
                  letter-spacing:1px;">{label}</p>
        <p style="margin:4px 0 0;color:{color};font-size:18px;font-weight:bold;
                  font-family:monospace;">{value}</p>
      </td>"""


def _signal_row(t: dict) -> str:
    pnl   = float(t.get("pnl_dollars") or 0)
    c     = _GREEN if pnl >= 0 else _RED
    icon  = "✅" if t["outcome"] == "win" else "❌"
    dir_c = _GREEN if t["direction"] == "BUY" else _RED
    sign  = "+" if pnl >= 0 else ""
    approved = "✓" if t.get("claude_approved") else "✗"
    app_c = _GREEN if t.get("claude_approved") else _RED
    return f"""
      <tr style="border-bottom:1px solid {_BORDER};">
        <td style="padding:6px 10px;color:{_SUB};font-size:11px;">{t.get('close_time_dt','')[:16]}</td>
        <td style="padding:6px 10px;color:{dir_c};font-family:monospace;font-size:12px;font-weight:bold;">
          {t['direction']}</td>
        <td style="padding:6px 10px;color:{_TEXT};font-family:monospace;font-size:12px;">
          ${float(t.get('entry_mid') or 0):.2f}</td>
        <td style="padding:6px 10px;color:{_TEXT};font-family:monospace;font-size:12px;">
          ${float(t.get('close_price') or 0):.2f}</td>
        <td style="padding:6px 10px;color:{c};font-weight:bold;font-family:monospace;font-size:12px;">
          {sign}${abs(pnl):.2f}</td>
        <td style="padding:6px 10px;color:{_SUB};font-size:11px;">
          {(t.get('key_level_type') or '—').replace('_',' ')}</td>
        <td style="padding:6px 10px;color:{app_c};font-size:12px;text-align:center;">{approved}</td>
        <td style="padding:6px 10px;">{icon}</td>
      </tr>"""


def build_email_html(
    daily: dict, accum: dict, balance: dict,
    today_trades: list[dict], open_sig: dict | None,
    report_date: str,
) -> str:
    bal         = float(balance.get("balance_after", 1000) or 1000)
    start_bal   = 1000.0
    overall_pnl = bal - start_bal
    overall_pct = (overall_pnl / start_bal) * 100
    overall_c   = _GREEN if overall_pnl >= 0 else _RED

    d_pnl = float(daily.get("total_pnl") or 0)
    d_n   = int(daily.get("total") or 0)
    d_wr  = float(daily.get("win_rate") or 0)
    d_c   = _GREEN if d_pnl >= 0 else _RED

    a_pnl  = float(accum.get("total_pnl") or 0)
    a_n    = int(accum.get("total") or 0)
    a_wr   = float(accum.get("win_rate") or 0)
    a_best = float(accum.get("best_trade") or 0)
    gp     = float(accum.get("gross_profit") or 0)
    gl     = float(accum.get("gross_loss") or 0)
    pf     = round(gp / max(gl, 0.01), 2) if gl else 0.0

    # Claude filter section
    c_app_n   = int(accum.get("claude_approved_n") or 0)
    c_rej_n   = int(accum.get("claude_rejected_n") or 0)
    c_app_pnl = float(accum.get("claude_approved_pnl") or 0)
    c_rej_pnl = float(accum.get("claude_rejected_pnl") or 0)
    c_app_wr  = accum.get("claude_approve_wr") or 0
    c_rej_wr  = accum.get("claude_reject_wr") or 0

    c_app_c = _GREEN if c_app_pnl >= 0 else _RED
    c_rej_c = _GREEN if c_rej_pnl >= 0 else _RED

    trade_rows = "".join(_signal_row(t) for t in today_trades)
    if not trade_rows:
        trade_rows = (f'<tr><td colspan="8" style="padding:18px;text-align:center;'
                      f'color:{_SUB};font-style:italic;">No signals closed today</td></tr>')

    open_banner = ""
    if open_sig:
        open_banner = f"""
  <tr><td style="background:#1e3a5f;padding:10px 24px;text-align:center;">
    <p style="margin:0;color:{_BLUE};font-size:12px;">
      🔄 1 signal currently open: SIG-{open_sig['id']:04d} {open_sig['direction']}
      entered at ${float(open_sig.get('entry_mid') or 0):.2f} @ {open_sig.get('created_at_dt','')[:16]}
    </p>
  </td></tr>"""

    pnl_sign = "+" if overall_pnl >= 0 else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Signal Generator Daily Report</title>
</head>
<body style="margin:0;padding:20px 10px;background:{_DARK};font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center">
<table width="660" cellpadding="0" cellspacing="0"
       style="max-width:660px;width:100%;border-radius:12px;
              box-shadow:0 4px 24px rgba(0,0,0,0.5);">

  <!-- Header -->
  <tr><td style="background:{_BG};border-radius:12px 12px 0 0;padding:28px 32px;
                 text-align:center;border-bottom:4px solid {_GOLD};">
    <h1 style="margin:0;color:{_GOLD};font-size:24px;font-weight:bold;
               letter-spacing:2px;font-family:Arial,sans-serif;">📊 SIGNAL GENERATOR</h1>
    <p style="margin:6px 0 2px;color:{_BLUE};font-size:15px;">Daily Report — {report_date}</p>
    <p style="margin:8px 0 0;color:{overall_c};font-size:22px;font-weight:bold;font-family:monospace;">
      ${bal:,.2f}
      <span style="font-size:13px;color:{overall_c};">({pnl_sign}${abs(overall_pnl):.2f} / {pnl_sign}{abs(overall_pct):.1f}% from $1,000 start)</span>
    </p>
  </td></tr>

  <!-- Today stats -->
  <tr><td style="background:{_CARD};padding:8px 16px;">
    <p style="margin:6px 0 4px;color:{_SUB};font-size:10px;text-transform:uppercase;letter-spacing:1px;">TODAY</p>
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_cell("SIGNALS", str(d_n))}
      {_stat_cell("TODAY P&amp;L", ('+' if d_pnl>=0 else '') + f'${abs(d_pnl):.2f}', d_c)}
      {_stat_cell("WIN RATE", f"{d_wr:.1f}%", _GREEN if d_wr>=50 else _RED)}
      <td style="border:none;"></td>
    </tr></table>
  </td></tr>

  <!-- Accumulated stats -->
  <tr><td style="background:{_BG};padding:8px 16px;">
    <p style="margin:6px 0 4px;color:{_SUB};font-size:10px;text-transform:uppercase;letter-spacing:1px;">ALL TIME</p>
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_cell("TOTAL SIGNALS", str(a_n))}
      {_stat_cell("TOTAL P&amp;L", ('+' if a_pnl>=0 else '') + f'${abs(a_pnl):.2f}', _GREEN if a_pnl>=0 else _RED)}
      {_stat_cell("WIN RATE", f"{a_wr:.1f}%", _GREEN if a_wr>=50 else _RED)}
      {_stat_cell("PROFIT FACTOR", f"{pf:.2f}", _GREEN if pf>=1 else _RED)}
      {_stat_cell("BEST TRADE", f'+${a_best:.2f}', _GREEN)}
    </tr></table>
  </td></tr>

  <!-- Claude filter -->
  <tr><td style="background:{_CARD};padding:12px 24px;">
    <p style="margin:0 0 8px;color:{_GOLD};font-size:12px;text-transform:uppercase;letter-spacing:1px;">🤖 Claude Filter Performance</p>
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_cell(f"APPROVED ({c_app_n})", ('+' if c_app_pnl>=0 else '') + f'${abs(c_app_pnl):.2f}', c_app_c)}
      {_stat_cell("APPROVED WIN%", f'{c_app_wr:.0f}%' if c_app_wr else '—', _GREEN if float(c_app_wr or 0)>=50 else _RED)}
      {_stat_cell(f"REJECTED ({c_rej_n})", ('+' if c_rej_pnl>=0 else '') + f'${abs(c_rej_pnl):.2f}', c_rej_c)}
      {_stat_cell("REJECTED WIN%", f'{c_rej_wr:.0f}%' if c_rej_wr else '—', _GREEN if float(c_rej_wr or 0)>=50 else _RED)}
    </tr></table>
  </td></tr>

  {open_banner}

  <!-- Today's signal table -->
  <tr><td style="background:{_BG};padding:16px 24px;">
    <h3 style="margin:0 0 12px;color:{_GOLD};font-size:13px;text-transform:uppercase;letter-spacing:1px;">
      Today's Signals ({len(today_trades)})</h3>
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
      <tr style="border-bottom:1px solid #6b7280;">
        <th style="padding:6px 10px;text-align:left;color:{_SUB};font-size:10px;text-transform:uppercase;">Time</th>
        <th style="padding:6px 10px;text-align:left;color:{_SUB};font-size:10px;text-transform:uppercase;">Dir</th>
        <th style="padding:6px 10px;text-align:left;color:{_SUB};font-size:10px;text-transform:uppercase;">Entry</th>
        <th style="padding:6px 10px;text-align:left;color:{_SUB};font-size:10px;text-transform:uppercase;">Close</th>
        <th style="padding:6px 10px;text-align:left;color:{_SUB};font-size:10px;text-transform:uppercase;">P&amp;L</th>
        <th style="padding:6px 10px;text-align:left;color:{_SUB};font-size:10px;text-transform:uppercase;">Level</th>
        <th style="padding:6px 10px;text-align:center;color:{_SUB};font-size:10px;text-transform:uppercase;">Claude</th>
        <th style="padding:6px 10px;text-align:center;color:{_SUB};font-size:10px;text-transform:uppercase;">Result</th>
      </tr>
      {trade_rows}
    </table>
  </td></tr>

  <!-- Footer -->
  <tr><td style="background:{_DARK};border-radius:0 0 12px 12px;padding:18px 32px;
                 text-align:center;border-top:1px solid {_BORDER};">
    <p style="margin:0;color:{_SUB};font-size:11px;">
      Signal Generator Virtual Account (XAUUSD, 0.1 lots, 1:500 leverage) &bull; Starting balance: $1,000</p>
    <p style="margin:6px 0 0;color:#4b5563;font-size:10px;">
      For information only. Virtual results do not guarantee live performance.</p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    from forex_trader.core import database as db_module
    from forex_trader.core import telegram_alerts
    from forex_trader.core import email_service

    # Point the core database module at the main app DB so Telegram/email
    # configs can be read. The test_signal DB is read directly via sqlite3 above.
    _MAIN_DB = str(DATA_DIR / "forex_trader_demo.db")
    db_module.init(_MAIN_DB)

    report_date = datetime.now().strftime("%A, %d %B %Y")
    today = date.today().isoformat()

    print(f"[signal_report] Generating report for {today}...")

    # Gather data
    today_trades = _get_today_signals()
    daily        = _get_stats(f"AND date(close_time,'unixepoch','localtime') = ?", (today,))
    accum        = _get_stats()
    balance      = _get_balance()
    open_sig     = _get_open_signal()

    print(f"[signal_report] Today: {len(today_trades)} signals  |  All-time: {accum.get('total',0)} signals")
    print(f"[signal_report] Balance: ${float(balance.get('balance_after', 1000) or 1000):,.2f}")

    # Build messages
    tg_text    = build_telegram_text(daily, accum, balance, today_trades, open_sig, report_date)
    email_html = build_email_html(daily, accum, balance, today_trades, open_sig, report_date)

    # Send Telegram
    tg_ok = await telegram_alerts.send_message(tg_text, event_type="signal_gen_daily_report")
    print(f"[signal_report] Telegram: {'✅ sent' if tg_ok else '❌ failed'}")

    # Send email
    subject = f"Signal Generator Report — {today}  |  Balance ${float(balance.get('balance_after', 1000) or 1000):,.2f}"
    email_ok, email_err = await email_service.send_email(subject, email_html)
    print(f"[signal_report] Email:    {'✅ sent' if email_ok else f'❌ failed: {email_err}'}")

    if tg_ok or email_ok:
        print("[signal_report] Report delivered successfully.")
    else:
        print("[signal_report] WARNING: both Telegram and email failed.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
