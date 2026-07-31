"""
Email service — daily and weekly HTML performance reports.
Uses Python stdlib smtplib so no extra dependencies required.
"""

import asyncio
import logging
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

log = logging.getLogger(__name__)

# ── Colour palette (matches app dark theme) ───────────────────────────────────
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


def _pnl_color(v: float) -> str:
    return _GREEN if v >= 0 else _RED


def _fmt(v: float, prefix: str = "$") -> str:
    sign = "+" if v >= 0 else "-"
    return f"{sign}{prefix}{abs(v):,.2f}"


def _stat_cell(label: str, value: str, color: str = _TEXT) -> str:
    return f"""
      <td align="center" style="padding:12px 8px;border-right:1px solid {_BORDER};">
        <p style="margin:0;color:{_SUB};font-size:10px;text-transform:uppercase;
                  letter-spacing:1px;">{label}</p>
        <p style="margin:4px 0 0;color:{color};font-size:18px;font-weight:bold;
                  font-family:monospace;">{value}</p>
      </td>"""


def _trade_row(t: dict) -> str:
    pnl = float(t.get("mt5_profit") or t.get("net_pnl") or 0)
    c   = _pnl_color(pnl)
    direction = t.get("direction", "?")
    dir_color = _GREEN if direction == "BUY" else _RED
    return f"""
      <tr style="border-bottom:1px solid {_BORDER};">
        <td style="padding:7px 12px;color:{dir_color};font-family:monospace;font-size:12px;
                   font-weight:bold;">{direction}</td>
        <td style="padding:7px 12px;color:{_TEXT};font-family:monospace;font-size:12px;">
          {float(t.get('entry_price', 0)):.2f}</td>
        <td style="padding:7px 12px;color:{_TEXT};font-family:monospace;font-size:12px;">
          {float(t.get('close_price', 0)):.2f}</td>
        <td style="padding:7px 12px;color:{_TEXT};font-family:monospace;font-size:12px;">
          {float(t.get('lot_size', 0)):.2f}</td>
        <td style="padding:7px 12px;color:{c};font-weight:bold;font-family:monospace;font-size:12px;">
          {_fmt(pnl)}</td>
        <td style="padding:7px 12px;color:{_SUB};font-size:11px;">
          {(t.get('exit_reason') or '—').replace('_', ' ')}</td>
      </tr>"""


def _header_html(subtitle: str, date_str: str) -> str:
    return f"""
  <tr><td style="background:{_BG};border-radius:12px 12px 0 0;padding:28px 32px;
                 text-align:center;border-bottom:4px solid {_GOLD};">
    <h1 style="margin:0;color:{_GOLD};font-size:26px;font-weight:bold;
               letter-spacing:2px;font-family:Arial,sans-serif;">FOREX TRADER</h1>
    <p style="margin:6px 0 2px;color:{_BLUE};font-size:15px;
              font-family:Arial,sans-serif;">{subtitle}</p>
    <p style="margin:0;color:{_SUB};font-size:12px;
              font-family:Arial,sans-serif;">{date_str}</p>
  </td></tr>"""


def _footer_html(report_type: str) -> str:
    return f"""
  <tr><td style="background:{_DARK};border-radius:0 0 12px 12px;padding:18px 32px;
                 text-align:center;border-top:1px solid {_BORDER};">
    <p style="margin:0;color:{_SUB};font-size:11px;font-family:Arial,sans-serif;">
      FOREX Trader by Simon Moore &bull; Automated {report_type} Report</p>
    <p style="margin:6px 0 0;color:#4b5563;font-size:10px;font-family:Arial,sans-serif;">
      This report is for information only. Past performance does not guarantee future results.</p>
  </td></tr>"""


# ── Daily summary ──────────────────────────────────────────────────────────────

def _verdict_color(v: str) -> str:
    return {"good": _GREEN, "ok": _GOLD, "poor": _RED}.get(v.lower(), _SUB)


def _claude_analysis_html(analysis: dict) -> str:
    if not analysis or not analysis.get("overall_summary"):
        return ""

    verdict_color = {
        "positive":  _GREEN,
        "negative":  _RED,
        "breakeven": _GOLD,
    }.get((analysis.get("overall_pnl_verdict") or "").lower(), _SUB)

    # Per-trade cards
    trade_cards = ""
    for ta in (analysis.get("trade_analysis") or []):
        vc = _verdict_color(ta.get("verdict", ""))
        better = ta.get("better_strategy", "")
        better_str = (
            f'<span style="color:{_GOLD};font-weight:bold;">{better}</span>'
            if better and better not in ("same", ta.get("trade_id", ""))
            else '<span style="color:{_SUB};">no change needed</span>'
        ).replace("{_SUB}", _SUB)
        est = ta.get("better_strategy_pnl_est", "")
        left = ta.get("left_on_table", "")
        trade_cards += f"""
        <tr style="border-bottom:1px solid {_BORDER};">
          <td style="padding:8px 10px;font-family:monospace;font-size:11px;
                     color:{vc};font-weight:bold;white-space:nowrap;">
            {ta.get('verdict','').upper()}</td>
          <td style="padding:8px 10px;color:{_SUB};font-family:monospace;font-size:11px;
                     white-space:nowrap;">{ta.get('trade_id','')[:12]}</td>
          <td style="padding:8px 10px;color:{_TEXT};font-size:11px;">
            {ta.get('max_tp_reached','—')}</td>
          <td style="padding:8px 10px;color:{_TEXT};font-size:11px;">
            {better_str}{f' <span style="color:{_GREEN};font-size:10px;">(~{est})</span>' if est else ''}</td>
          <td style="padding:8px 10px;color:{_GOLD};font-size:11px;font-style:italic;">
            {left or '—'}</td>
          <td style="padding:8px 10px;color:{_SUB};font-size:11px;">
            {ta.get('note','')}</td>
        </tr>"""

    if not trade_cards:
        trade_cards = (
            f'<tr><td colspan="6" style="padding:14px;text-align:center;'
            f'color:{_SUB};font-style:italic;">No trade-level analysis available</td></tr>'
        )

    # Tomorrow focus bullets
    focus_items = "".join(
        f'<li style="margin:4px 0;color:{_TEXT};font-size:12px;">{f}</li>'
        for f in (analysis.get("tomorrow_focus") or [])
    )
    focus_html = (
        f'<ul style="margin:8px 0 0 16px;padding:0;">{focus_items}</ul>'
        if focus_items else ""
    )

    best_strat = analysis.get("best_strategy_today", "")
    best_reason = analysis.get("best_strategy_reason", "")
    key_lesson = analysis.get("key_lesson", "")

    tomorrow_section = (
        f'<tr><td style="background:{_DARK};padding:12px 24px 16px;">'
        f'<p style="margin:0;color:{_SUB};font-size:9px;text-transform:uppercase;'
        f'letter-spacing:1px;">Tomorrow\'s Focus</p>'
        + focus_html +
        '</td></tr>'
    ) if focus_html else ""

    return f"""
  <!-- Claude analysis section -->
  <tr><td style="background:{_DARK};padding:20px 24px 8px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td style="border-left:3px solid #a855f7;padding:0 0 0 12px;">
        <p style="margin:0;color:#a855f7;font-size:10px;text-transform:uppercase;
                  letter-spacing:2px;font-weight:bold;">Claude AI Coaching</p>
        <p style="margin:4px 0 0;color:{verdict_color};font-size:13px;font-weight:bold;">
          {analysis.get('overall_summary','')}</p>
      </td></tr>
    </table>
  </td></tr>

  <!-- Trade-level breakdown -->
  <tr><td style="background:{_BG};padding:4px 24px 16px;">
    <h3 style="margin:12px 0 8px;color:#a855f7;font-size:11px;
               text-transform:uppercase;letter-spacing:1px;">Trade Breakdown</h3>
    <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;">
      <tr style="border-bottom:1px solid #6b7280;">
        <th style="padding:5px 10px;text-align:left;color:{_SUB};font-size:9px;
                   text-transform:uppercase;">Grade</th>
        <th style="padding:5px 10px;text-align:left;color:{_SUB};font-size:9px;
                   text-transform:uppercase;">Trade</th>
        <th style="padding:5px 10px;text-align:left;color:{_SUB};font-size:9px;
                   text-transform:uppercase;">Max TP Reached</th>
        <th style="padding:5px 10px;text-align:left;color:{_SUB};font-size:9px;
                   text-transform:uppercase;">Better Strategy</th>
        <th style="padding:5px 10px;text-align:left;color:{_SUB};font-size:9px;
                   text-transform:uppercase;">Left on Table</th>
        <th style="padding:5px 10px;text-align:left;color:{_SUB};font-size:9px;
                   text-transform:uppercase;">Note</th>
      </tr>
      {trade_cards}
    </table>
  </td></tr>

  <!-- Best strategy + key lesson -->
  <tr><td style="background:{_CARD};padding:16px 24px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td width="50%" style="padding-right:12px;vertical-align:top;">
        <p style="margin:0;color:{_SUB};font-size:9px;text-transform:uppercase;
                  letter-spacing:1px;">Best Strategy Today</p>
        <p style="margin:4px 0 2px;color:{_GOLD};font-size:14px;font-weight:bold;">
          {best_strat or '—'}</p>
        <p style="margin:0;color:{_TEXT};font-size:11px;">{best_reason}</p>
      </td>
      <td width="50%" style="padding-left:12px;vertical-align:top;
                              border-left:1px solid {_BORDER};">
        <p style="margin:0;color:{_SUB};font-size:9px;text-transform:uppercase;
                  letter-spacing:1px;">Key Lesson for Tomorrow</p>
        <p style="margin:4px 0 0;color:{_TEXT};font-size:12px;font-style:italic;">
          {key_lesson or '—'}</p>
      </td>
    </tr></table>
  </td></tr>

  <!-- Tomorrow's focus -->
  {tomorrow_section}"""


def build_daily_html(
    perf: dict,
    closed_today: list[dict],
    date_str: str = "",
    claude_analysis: Optional[dict] = None,
) -> str:
    date_str  = date_str or datetime.now().strftime("%A, %d %B %Y")
    balance   = float(perf.get("balance", 0) or 0)
    equity    = float(perf.get("equity", 0) or 0)
    daily_pnl = float(perf.get("daily_pnl", 0) or 0)
    win_rate  = float(perf.get("daily_win_rate_pct", 0) or 0)
    open_ct   = int(perf.get("open_trades", 0) or 0)
    closed_ct = len(closed_today)

    # Profit factor from today's closed trades only
    _day_wins   = sum(float(t.get("net_pnl") or 0) for t in closed_today if float(t.get("net_pnl") or 0) > 0)
    _day_losses = abs(sum(float(t.get("net_pnl") or 0) for t in closed_today if float(t.get("net_pnl") or 0) < 0))
    pf = round(_day_wins / max(_day_losses, 0.01), 2) if closed_today else 0.0

    pnl_color = _pnl_color(daily_pnl)
    pnl_str   = _fmt(daily_pnl)

    trade_rows_html = "".join(_trade_row(t) for t in closed_today[:25])
    if not trade_rows_html:
        trade_rows_html = (
            f'<tr><td colspan="6" style="padding:18px;text-align:center;'
            f'color:{_SUB};font-style:italic;">No trades closed today</td></tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>FOREX Trader Daily Summary</title>
</head>
<body style="margin:0;padding:20px 10px;background:{_DARK};font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0"
       style="max-width:620px;width:100%;border-radius:12px;
              box-shadow:0 4px 24px rgba(0,0,0,0.5);">

  {_header_html("Daily Performance Summary", date_str)}

  <!-- Key stats -->
  <tr><td style="background:{_CARD};padding:8px 16px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_cell("BALANCE", f"${balance:,.2f}")}
      {_stat_cell("EQUITY",  f"${equity:,.2f}")}
      {_stat_cell("TODAY'S P&amp;L", pnl_str, pnl_color)}
      <td style="border:none;"></td>
    </tr></table>
  </td></tr>

  <!-- Performance row -->
  <tr><td style="background:{_BG};padding:8px 16px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_cell("WIN RATE",     f"{win_rate:.1f}%",    _GREEN)}
      {_stat_cell("PROFIT FACTOR", f"{pf:.2f}")}
      {_stat_cell("OPEN TRADES",  str(open_ct))}
      {_stat_cell("TOTAL CLOSED", str(closed_ct))}
    </tr></table>
  </td></tr>

  <!-- Today's trades table -->
  <tr><td style="background:{_CARD};padding:16px 24px;">
    <h3 style="margin:0 0 12px;color:{_GOLD};font-size:13px;
               text-transform:uppercase;letter-spacing:1px;">
      Today's Closed Trades ({len(closed_today)})</h3>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;">
      <tr style="border-bottom:1px solid #6b7280;">
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">Dir</th>
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">Entry</th>
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">Close</th>
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">Lots</th>
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">P&amp;L</th>
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">Exit</th>
      </tr>
      {trade_rows_html}
    </table>
  </td></tr>

  {_claude_analysis_html(claude_analysis) if claude_analysis else ''}

  {_footer_html("Daily")}

</table>
</td></tr>
</table>
</body>
</html>"""


# ── Weekly summary ─────────────────────────────────────────────────────────────

def build_weekly_html(
    perf: dict,
    week_trades: list[dict],
    week_label: str = "",
) -> str:
    week_label  = week_label or f"Week ending {datetime.now().strftime('%d %B %Y')}"
    balance     = float(perf.get("balance", 0) or 0)
    equity      = float(perf.get("equity", 0) or 0)
    total_pnl   = float(perf.get("total_net_pnl", 0) or 0)
    win_rate    = float(perf.get("win_rate_pct", 0) or 0)
    pf          = float(perf.get("profit_factor", 0) or 0)
    best        = float(perf.get("best_trade", 0) or 0)
    worst       = float(perf.get("worst_trade", 0) or 0)
    max_dd      = float(perf.get("max_drawdown_pct", 0) or 0)

    total_color = _pnl_color(total_pnl)
    total_str   = _fmt(total_pnl)

    # Week P&L from week_trades list
    week_pnl = sum(float(t.get("mt5_profit") or t.get("net_pnl") or 0) for t in week_trades)
    week_color = _pnl_color(week_pnl)

    trade_rows_html = "".join(_trade_row(t) for t in week_trades[:30])
    if not trade_rows_html:
        trade_rows_html = (
            f'<tr><td colspan="6" style="padding:18px;text-align:center;'
            f'color:{_SUB};font-style:italic;">No trades this week</td></tr>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>FOREX Trader Weekly Summary</title>
</head>
<body style="margin:0;padding:20px 10px;background:{_DARK};font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0"
       style="max-width:620px;width:100%;border-radius:12px;
              box-shadow:0 4px 24px rgba(0,0,0,0.5);">

  {_header_html("Weekly Performance Summary", week_label)}

  <!-- Key stats -->
  <tr><td style="background:{_CARD};padding:8px 16px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_cell("BALANCE", f"${balance:,.2f}")}
      {_stat_cell("EQUITY",  f"${equity:,.2f}")}
      {_stat_cell("WEEK P&amp;L",  _fmt(week_pnl),  week_color)}
      {_stat_cell("TOTAL P&amp;L (90d)", total_str, total_color)}
    </tr></table>
  </td></tr>

  <!-- Performance row -->
  <tr><td style="background:{_BG};padding:8px 16px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_cell("WIN RATE",     f"{win_rate:.1f}%", _GREEN)}
      {_stat_cell("PROFIT FACTOR", f"{pf:.2f}")}
      {_stat_cell("BEST TRADE",   _fmt(best),  _GREEN)}
      {_stat_cell("WORST TRADE",  _fmt(worst), _pnl_color(worst))}
      <td style="padding:12px 8px;text-align:center;">
        <p style="margin:0;color:{_SUB};font-size:10px;text-transform:uppercase;
                  letter-spacing:1px;">MAX DD</p>
        <p style="margin:4px 0 0;color:{_RED};font-size:18px;font-weight:bold;
                  font-family:monospace;">{max_dd:.1f}%</p>
      </td>
    </tr></table>
  </td></tr>

  <!-- Trades banner -->
  <tr><td style="background:{_CARD};padding:6px 24px;">
    <h3 style="margin:8px 0;color:{_GOLD};font-size:13px;
               text-transform:uppercase;letter-spacing:1px;">
      This Week's Trades ({len(week_trades)})</h3>
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;">
      <tr style="border-bottom:1px solid #6b7280;">
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">Dir</th>
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">Entry</th>
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">Close</th>
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">Lots</th>
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">P&amp;L</th>
        <th style="padding:7px 12px;text-align:left;color:{_SUB};
                   font-size:10px;text-transform:uppercase;">Exit</th>
      </tr>
      {trade_rows_html}
    </table>
  </td></tr>

  {_footer_html("Weekly")}

</table>
</td></tr>
</table>
</body>
</html>"""


# ── Morning ORB / IVB report ────────────────────────────────────────────────
# Adapted from Faber Vaale's "The Simplest Orderflow Trading Model"
# (youtube.com/watch?v=cUTsoU-15Tc) — see build_orb_report() in core/engine.py
# for the full methodology notes and what's deliberately not implemented
# (tick-level order-flow confirmation).

_ORB_CHART_CID = "orb_chart"

_DIRECTION_LABEL = {"bullish": ("Bullish breakout", _GREEN),
                    "bearish": ("Bearish breakout", _RED),
                    "inside":  ("Inside range — no breakout yet", _GOLD)}


def build_orb_chart_image(report: dict) -> Optional[bytes]:
    """
    Renders the opening range, volume-profile POC/value-area, and (once
    broken) the entry/stop/target zone as a PNG for inline embedding.
    Returns None if matplotlib isn't available or there's nothing to plot —
    callers must handle a None return (send without the chart) rather than
    treat it as an error, since a missing chart shouldn't block the report.
    """
    candles = report.get("candles") or []
    if not candles:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
        from matplotlib.dates import DateFormatter, date2num
        import io
        from datetime import datetime as _dt, timezone as _tz
    except ImportError:
        log.debug("[ORB] matplotlib not installed — sending report without a chart")
        return None

    try:
        times = [_dt.fromtimestamp(c["ts"], tz=_tz.utc) for c in candles]
        x = date2num(times)
        width = (x[1] - x[0]) * 0.7 if len(x) > 1 else 0.0006
        x_pad = (x[-1] - x[0]) * 0.18 if len(x) > 1 else 0.01  # room for right-edge annotations

        fig, ax = plt.subplots(figsize=(9, 5), dpi=150)
        fig.patch.set_facecolor(_DARK)
        ax.set_facecolor(_DARK)
        ax.set_xlim(x[0] - width, x[-1] + x_pad)

        for xi, c in zip(x, candles):
            o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
            color = _GREEN if cl >= o else _RED
            ax.add_line(plt.Line2D([xi, xi], [l, h], color=color, linewidth=0.8))
            body_bottom = min(o, cl)
            body_height = max(abs(cl - o), 0.02)
            ax.add_patch(Rectangle((xi - width / 2, body_bottom), width, body_height,
                                    facecolor=color, edgecolor=color))

        r_start = date2num(_dt.fromtimestamp(report["window_start"], tz=_tz.utc))
        r_end   = date2num(_dt.fromtimestamp(report["window_end"], tz=_tz.utc))
        ax.add_patch(Rectangle(
            (r_start, report["range_low"]), r_end - r_start,
            report["range_height"], facecolor=_GOLD, alpha=0.15,
            edgecolor=_GOLD, linewidth=1, zorder=0,
        ))

        ax.axhline(report["poc"], color="#a855f7", linewidth=1.3, linestyle="--")
        ax.axhline(report["vah"], color=_BLUE, linewidth=0.8, linestyle=":")
        ax.axhline(report["val"], color=_BLUE, linewidth=0.8, linestyle=":")
        ax.text(x[-1], report["poc"], " POC", color="#a855f7", fontsize=8, va="center")

        direction = report.get("direction", "inside")
        label, label_color = _DIRECTION_LABEL.get(direction, ("", _SUB))

        if direction in ("bullish", "bearish"):
            box_color = _GREEN if direction == "bullish" else _RED
            ax.axhspan(report["entry_zone_low"], report["entry_zone_high"],
                       xmin=(r_end - x[0]) / max(x[-1] - x[0], 1e-9), xmax=1,
                       facecolor=box_color, alpha=0.12)
            ax.axhline(report["target"], color=_GREEN, linewidth=1.3)
            ax.axhline(report["stop"], color=_RED, linewidth=1.3)
            ax.text(x[-1], report["target"], f" Target ${report['target']:.2f}",
                    color=_GREEN, fontsize=8, va="bottom")
            ax.text(x[-1], report["stop"], f" Stop ${report['stop']:.2f}",
                    color=_RED, fontsize=8, va="top")
            rr = report.get("rr")
            title = f"London ORB/IVB — {label}" + (f" — R:R {rr:.2f}:1" if rr else "")
        else:
            title = f"London ORB/IVB — {label}"

        ax.set_title(title, color=_GOLD, fontsize=11, loc="left", fontweight="bold")
        ax.xaxis_date()
        ax.xaxis.set_major_formatter(DateFormatter("%H:%M", tz=_tz.utc))
        for spine in ax.spines.values():
            spine.set_color(_BORDER)
        ax.tick_params(colors=_SUB, labelsize=8)
        ax.grid(True, color=_CARD, linewidth=0.6, alpha=0.6)
        fig.subplots_adjust(left=0.09, right=0.99, top=0.90, bottom=0.10)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        log.warning("[ORB] chart render failed: %s", e)
        return None


def build_orb_html(report: dict, date_str: str = "", has_chart: bool = False) -> str:
    date_str = date_str or datetime.now().strftime("%A, %d %B %Y")

    current_price = float(report.get("current_price", 0) or 0)
    range_high   = report.get("range_high")
    range_low    = report.get("range_low")
    range_height = report.get("range_height")
    poc, vah, val = report.get("poc"), report.get("vah"), report.get("val")
    direction    = report.get("direction", "inside")
    position_note = report.get("position_note", "")

    label, label_color = _DIRECTION_LABEL.get(direction, ("", _SUB))

    range_str = (
        f"${range_low:.2f} – ${range_high:.2f}  ({range_height:.1f} pts)"
        if range_high is not None and range_low is not None else "not available"
    )
    profile_str = (
        f"POC ${poc:.2f}  ·  Value Area ${val:.2f} – ${vah:.2f}"
        if poc is not None else "not available"
    )

    chart_html = (
        f'<tr><td style="background:{_DARK};padding:8px 16px 0;text-align:center;">'
        f'<img src="cid:{_ORB_CHART_CID}" alt="ORB chart" '
        f'style="max-width:100%;border-radius:6px;" /></td></tr>'
        if has_chart else ""
    )

    if direction in ("bullish", "bearish"):
        rr = report.get("rr")
        n = report.get("target_sample_n", 0)
        is_default = report.get("target_is_default", True)
        confidence_note = (
            f"default 2.0x multiple — only {n} clean-breakout day(s) measured so far, "
            f"building history daily" if is_default else
            f"empirically measured from the last {n} clean-breakout days on this account"
        )
        trade_section = f"""
  <tr><td style="background:{_BG};padding:16px 24px;">
    <h3 style="margin:0 0 12px;color:{_GOLD};font-size:13px;
               text-transform:uppercase;letter-spacing:1px;">Reload Zone Setup</h3>
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_cell("ENTRY ZONE", f"${report['entry_zone_low']:.2f}–${report['entry_zone_high']:.2f}")}
      {_stat_cell("STOP", f"${report['stop']:.2f}", _RED)}
      {_stat_cell("TARGET", f"${report['target']:.2f}", _GREEN)}
      {_stat_cell("R:R", f"{rr:.2f}:1" if rr else "—", _GOLD)}
    </tr></table>
    <p style="margin:12px 0 0;color:{_SUB};font-size:11px;">
      Target = breakout level ± {report.get('target_multiple', 2.0):.2f}× the Asian
      range height ({confidence_note}).
      Stop = breakout level ± {report.get('sl_range_pct', 0.50) * 100:.0f}% of the Asian range height.
    </p>
  </td></tr>"""
    else:
        trade_section = f"""
  <tr><td style="background:{_BG};padding:16px 24px;">
    <p style="margin:0;color:{_SUB};font-size:12px;font-style:italic;">
      No breakout yet — waiting for price to clear the Asian range before a
      reload-zone setup can be framed.</p>
  </td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>FOREX Trader — London ORB/IVB Report</title>
</head>
<body style="margin:0;padding:20px 10px;background:{_DARK};font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0"
       style="max-width:620px;width:100%;border-radius:12px;
              box-shadow:0 4px 24px rgba(0,0,0,0.5);">

  {_header_html("London Open — ORB/IVB Report", date_str)}

  <!-- Current price + direction -->
  <tr><td style="background:{_CARD};padding:8px 16px;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      {_stat_cell("CURRENT PRICE", f"${current_price:.2f}")}
      {_stat_cell("STATUS", label, label_color)}
      <td style="border:none;"></td>
      <td style="border:none;"></td>
    </tr></table>
  </td></tr>

  {chart_html}

  <!-- Pre-London reference range + volume profile (the last hour before
       London open, breakout evaluated within the first 15 min of London) -->
  <tr><td style="background:{_BG};padding:16px 24px;">
    <h3 style="margin:0 0 8px;color:{_GOLD};font-size:13px;
               text-transform:uppercase;letter-spacing:1px;">Pre-London Range (last 1h before London open)</h3>
    <p style="margin:0 0 6px;color:{_TEXT};font-size:16px;font-family:monospace;font-weight:bold;">
      {range_str}</p>
    <p style="margin:0 0 6px;color:{_TEXT};font-size:13px;font-family:monospace;">
      {profile_str}</p>
    <p style="margin:0 0 6px;color:{_SUB};font-size:13px;">{position_note}</p>
  </td></tr>

  {trade_section}

  <tr><td style="background:{_DARK};padding:10px 24px;">
    <p style="margin:0;color:#4b5563;font-size:10px;">
      ORB (Opening Range Breakout) + IVB (Initial Value Balance) method adapted
      from Faber Vaale's "The Simplest Orderflow Trading Model" — opening-range
      breakout for direction, volume-profile framing for the reload-zone entry.
      Order-flow (footprint) confirmation is not implemented.
      Volume is MT5 tick-volume (a broker-side proxy), not exchange volume.</p>
  </td></tr>

  {_footer_html("ORB")}

</table>
</td></tr>
</table>
</body>
</html>"""


# ── Send ──────────────────────────────────────────────────────────────────────

def _make_ssl_context() -> ssl.SSLContext:
    """
    Build an SSL context that works on macOS Python.org installs.
    The framework Python ships without system CA certs installed; certifi
    provides a complete, up-to-date bundle that resolves the common
    'certificate verify failed: unable to get local issuer certificate' error.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    # Fallback: macOS system keychain bundle
    system_pem = "/etc/ssl/cert.pem"
    if ssl.os.path.exists(system_pem):
        return ssl.create_default_context(cafile=system_pem)
    return ssl.create_default_context()


def _send_sync(
    host: str, port: int, user: str, password: str,
    use_tls: bool, from_addr: str, to_addr: str,
    msg: MIMEMultipart,
) -> None:
    context = _make_ssl_context()
    if use_tls and port == 465:
        # Implicit TLS (SSL from the start)
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as smtp:
            if user and password:
                smtp.login(user, password)
            smtp.sendmail(from_addr, [to_addr], msg.as_string())
    else:
        # Explicit TLS (STARTTLS upgrade on port 587 or 25)
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            if use_tls:
                smtp.starttls(context=context)
                smtp.ehlo()
            if user and password:
                smtp.login(user, password)
            smtp.sendmail(from_addr, [to_addr], msg.as_string())


# Personal email domains that Resend cannot use as a verified sender domain.
# When the from_addr belongs to one of these, fall back to onboarding@resend.dev.
_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "live.com", "live.co.uk",
    "msn.com", "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com",
    "mac.com", "protonmail.com", "proton.me", "aol.com", "zoho.com",
}


def _resend_sender(from_addr: str) -> str:
    """Return a Resend-compatible from address.

    Personal email domains (gmail, outlook, etc.) cannot be verified as Resend
    sender domains, so we fall back to the shared onboarding@resend.dev address.
    """
    if not from_addr:
        return "onboarding@resend.dev"
    domain = from_addr.split("@")[-1].lower() if "@" in from_addr else ""
    if domain in _PERSONAL_EMAIL_DOMAINS:
        return "onboarding@resend.dev"
    return from_addr


async def _send_via_resend(
    subject: str, html_body: str, cfg: dict,
    image_bytes: Optional[bytes] = None, image_cid: str = "", image_filename: str = "chart.png",
) -> tuple[bool, str]:
    """
    Send via Resend API (resend.com).
    Free tier: 3,000 emails/month.  No SMTP, no app passwords.
    From address: use  onboarding@resend.dev  (Resend shared domain — no setup needed)
    OR your own domain once verified in the Resend dashboard.
    """
    import base64
    import httpx as _httpx

    api_key   = (cfg.get("resend_api_key") or "").strip()
    to_addr   = (cfg.get("to_addr") or "").strip()
    from_addr = _resend_sender((cfg.get("from_addr") or "").strip())

    if not api_key:
        return False, "Resend API key is required"
    if not to_addr:
        return False, "Recipient address (To) is required"

    from_field = f"FOREX Trader <{from_addr}>"

    payload = {
        "from":    from_field,
        "to":      [to_addr],
        "subject": subject,
        "html":    html_body,
    }
    if image_bytes and image_cid:
        # Resend's documented inline-image schema: attachments[].content_id
        # referenced from the HTML via <img src="cid:...">.
        payload["attachments"] = [{
            "filename":     image_filename,
            "content":      base64.b64encode(image_bytes).decode(),
            "content_type": "image/png",
            "content_id":   image_cid,
        }]
    try:
        async with _httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if r.status_code in (200, 201):
            log.info("Resend sent: %r → %s", subject, to_addr)
            return True, ""
        # If domain validation fails despite our check, retry with the shared sender.
        if r.status_code == 403 and "domain" in r.text.lower() and from_addr != "onboarding@resend.dev":
            log.warning("Resend domain not verified (%s), retrying with onboarding@resend.dev", from_addr)
            payload["from"] = "FOREX Trader <onboarding@resend.dev>"
            async with _httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    "https://api.resend.com/emails",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if r.status_code in (200, 201):
                log.info("Resend sent (fallback sender): %r → %s", subject, to_addr)
                return True, ""
        return False, f"Resend HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        return False, f"Resend error: {e}"


async def _send_via_mailjet(
    subject: str, html_body: str, cfg: dict,
    image_bytes: Optional[bytes] = None, image_cid: str = "", image_filename: str = "chart.png",
) -> tuple[bool, str]:
    """Send via Mailjet REST API — no SMTP, no port issues, 1500 free/month."""
    import httpx as _httpx

    api_key    = (cfg.get("mailjet_api_key") or "").strip()
    secret_key = (cfg.get("mailjet_secret_key") or "").strip()
    from_addr  = (cfg.get("from_addr") or cfg.get("smtp_user") or "").strip()
    to_addr    = (cfg.get("to_addr") or "").strip()

    if not api_key or not secret_key:
        return False, "Mailjet API key and Secret key are required"
    if not from_addr or not to_addr:
        return False, "From and To addresses are required"
    if image_bytes and image_cid:
        # Mailjet's inline-attachment schema isn't verified against their docs
        # here (unlike Resend's) since neither node currently uses Mailjet for
        # delivery — sending the text without the embedded chart rather than
        # guess at the field names.
        log.info("Mailjet: inline chart image not attached (schema not verified) — sending text only")

    payload = {
        "Messages": [{
            "From":    {"Email": from_addr, "Name": "FOREX Trader"},
            "To":      [{"Email": to_addr,   "Name": to_addr}],
            "Subject": subject,
            "HTMLPart": html_body,
        }]
    }
    try:
        async with _httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.mailjet.com/v3.1/send",
                json=payload,
                auth=(api_key, secret_key),
            )
        if r.status_code == 200:
            result = r.json()
            sent = result.get("Messages", [{}])[0]
            if sent.get("Status") == "success":
                log.info("Mailjet sent: %r → %s", subject, to_addr)
                return True, ""
            return False, f"Mailjet status: {sent.get('Status')} — {sent.get('Errors', '')}"
        return False, f"Mailjet HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"Mailjet error: {e}"


async def send_email(
    subject: str,
    html_body: str,
    cfg: Optional[dict] = None,
    *,
    image_bytes: Optional[bytes] = None,
    image_cid: str = "",
    image_filename: str = "chart.png",
) -> tuple[bool, str]:
    """
    Send an HTML email. Returns (success, error_message).

    Routing:
      - Explicit cfg (from test buttons) routes by which keys are present.
      - Scheduled sends (cfg=None) route by the saved send_provider setting.

    image_bytes/image_cid: optional inline image (e.g. a PNG chart) referenced
    from html_body via <img src="cid:{image_cid}">. Supported on Resend and
    SMTP; silently omitted (text still sends) on Mailjet — see
    _send_via_mailjet's docstring for why.
    """
    from backend.src.db import database as db_module
    if cfg is None:
        cfg = db_module.get_email_config()

    # Tag every outgoing email with which node sent it (Local Mac vs Remote
    # VPS) — same flag the UI's Local/Remote toggle and run.py's browser-skip
    # logic already use, so it always matches what the header shows.
    node_label = "Remote (VPS)" if db_module.get_app_config("sync_server_enabled") == "1" else "Local"
    subject = f"[{node_label}] {subject}"
    html_body = (
        f'<div style="background:#111827;color:#9ca3af;font-size:11px;'
        f'font-family:Arial,sans-serif;padding:6px 12px;text-align:center;">'
        f'Sent from: {node_label} node</div>'
    ) + html_body

    # When a test button passes an explicit cfg_snap it will only contain the
    # keys for that provider, so the checks below route correctly. For scheduled
    # sends the full db config is used and send_provider steers the choice.
    send_provider = (cfg.get("send_provider") or "").strip()

    # ── Resend path ───────────────────────────────────────────────────────────
    has_resend = bool((cfg.get("resend_api_key") or "").strip())
    if has_resend and (not send_provider or send_provider == "resend"):
        return await _send_via_resend(subject, html_body, cfg, image_bytes, image_cid, image_filename)

    # ── Mailjet path ──────────────────────────────────────────────────────────
    has_mailjet = bool((cfg.get("mailjet_api_key") or "").strip())
    if has_mailjet and send_provider == "mailjet":
        return await _send_via_mailjet(subject, html_body, cfg, image_bytes, image_cid, image_filename)

    # ── SMTP path ─────────────────────────────────────────────────────────────
    host     = (cfg.get("smtp_host") or "").strip()
    to_addr  = (cfg.get("to_addr") or "").strip()
    if not host:
        if has_resend:
            # send_provider is set to SMTP but SMTP host is missing — fall back to Resend
            log.warning("send_provider=%r but no SMTP host configured; falling back to Resend", send_provider)
            return await _send_via_resend(subject, html_body, cfg, image_bytes, image_cid, image_filename)
        return False, "SMTP host not configured"
    if not to_addr:
        return False, "Recipient address not configured"

    port      = int(cfg.get("smtp_port") or 587)
    user      = (cfg.get("smtp_user") or "").strip()
    password  = (cfg.get("smtp_password") or "").strip()
    from_addr = (cfg.get("from_addr") or user or "").strip()
    use_tls   = bool(cfg.get("use_tls", 1))

    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(html_body, "html", "utf-8"))

    if image_bytes and image_cid:
        from email.mime.image import MIMEImage
        msg = MIMEMultipart("related")
        msg.attach(alt_part)
        img_part = MIMEImage(image_bytes, "png")
        img_part.add_header("Content-ID", f"<{image_cid}>")
        img_part.add_header("Content-Disposition", "inline", filename=image_filename)
        msg.attach(img_part)
    else:
        msg = alt_part

    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _send_sync(host, port, user, password, use_tls, from_addr, to_addr, msg),
        )
        log.info("Email sent (SMTP): %r → %s", subject, to_addr)
        return True, ""
    except Exception as e:
        log.error("SMTP send failed: %s", e)
        return False, str(e)
