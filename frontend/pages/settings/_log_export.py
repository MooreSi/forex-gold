"""The Export Logs action: read the log file, filter it, and mail it out.

Lifted out of _render_diagnostics so that module stays under the 800-line
ceiling. It was a closure over exactly one name -- the status label it writes
progress into -- which is now its only parameter. Nothing else about it
changed.
"""
from nicegui import ui

from backend.src.controllers import settings_controller as settings_ctl


async def export_logs(export_lbl) -> None:
    import base64 as _b64
    import platform as _platform
    import sys as _sys
    import time as _time
    from datetime import datetime as _dt
    from pathlib import Path as _Path
    from backend.src.controllers import settings_controller as _db_ctl
    import httpx as _httpx

    DAYS       = 5
    MAX_BYTES  = 25 * 1024 * 1024   # 25 MB raw → ~33 MB base64 → well under Resend 40 MB cap
    TO = "simon.moore@outlook.com"

    # ── Polling endpoints that produce only noise ─────────────────────────
    # These are httpx GET lines that fire every few seconds continuously.
    _POLL_DROPS = (
        "/tick/XAUUSD",
        "/candles/XAUUSD",
        "/candles_symbol/",
        "localhost:9000/positions ",
        "localhost:9000/account ",
        "localhost:9000/health",
        "/history?days=",
        "/history/position/",
        "getUpdates",
    )

    def _keep_line(ln: str) -> bool:
        """Return True only for log lines worth including in a troubleshooting export."""
        # Drop all DEBUG lines
        if " DEBUG " in ln:
            return False
        # Drop httpx GET polling lines — these are background heartbeats
        if "INFO httpx" in ln and "HTTP Request: GET" in ln:
            for _drop in _POLL_DROPS:
                if _drop in ln:
                    return False
        return True

    export_lbl.text = "Reading and filtering logs..."
    export_lbl.classes(replace="text-sm mt-1 text-gray-400")

    try:
        # ── Collect log files (current + rotated backups) ─────────────────
        from backend.src.controllers.settings_controller import DATA_DIR as _log_data_dir
        log_base = _Path(_log_data_dir) / "forex_trader.log"
        if not log_base.exists():
            export_lbl.text = "No log file found — restart the app first to begin writing logs."
            export_lbl.classes(replace="text-sm mt-1 text-orange-400")
            return

        # TimedRotatingFileHandler names backups forex_trader.log.YYYY-MM-DD.
        # Ascending sort gives chronological order (oldest date first),
        # then append the current (today's) file at the end.
        candidates = sorted(
            log_base.parent.glob("forex_trader.log.2*"),
        ) + [log_base]

        cutoff    = _time.time() - DAYS * 86400
        raw_total = 0       # total lines seen (before filter)
        lines     = []      # filtered lines to include

        for path in candidates:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        stripped = line.rstrip()
                        if not stripped:
                            continue
                        raw_total += 1
                        # Date-range filter
                        try:
                            ts = _dt.strptime(stripped[:23], "%Y-%m-%d %H:%M:%S,%f").timestamp()
                            if ts < cutoff:
                                continue
                        except Exception:
                            pass  # non-timestamped lines (tracebacks etc) — keep
                        if _keep_line(stripped):
                            lines.append(stripped)
            except Exception:
                pass  # skip unreadable rotation files

        if not lines:
            export_lbl.text = (
                f"No meaningful log entries in the last {DAYS} days "
                f"({raw_total:,} lines scanned — all were routine polling)."
            )
            export_lbl.classes(replace="text-sm mt-1 text-orange-400")
            return

        # ── Deduplicate consecutive repeated lines ────────────────────────
        # httpx POST /modify fires every 5 s while a trade is live — collapse those.
        deduped: list[str] = []
        prev_body  = None
        repeat_cnt = 0
        for ln in lines:
            body = ln[24:] if len(ln) > 24 else ln   # strip timestamp
            if body == prev_body:
                repeat_cnt += 1
            else:
                if repeat_cnt > 0:
                    deduped.append(f"        ... repeated {repeat_cnt} more time(s)")
                deduped.append(ln)
                prev_body  = body
                repeat_cnt = 0
        if repeat_cnt > 0:
            deduped.append(f"        ... repeated {repeat_cnt} more time(s)")

        filtered_total = len(deduped)
        now_str = _dt.now().strftime("%Y-%m-%d %H:%M")
        fname   = f"forex_trader_logs_{_dt.now().strftime('%Y%m%d_%H%M')}.txt"

        # ── Gather user and system info ────────────────────────────────────
        try:
            from backend.src.config.licence import store as _lic_store
            from backend.src.config.licence.fingerprint import get_fingerprint as _fp
            _lic_data    = _lic_store.load() or {}
            _lic_email   = _lic_data.get("email", "—")
            _lic_type    = _lic_data.get("licence_type", "—")
            _lic_expiry  = _lic_data.get("expiry_date", "—")
            _machine_id  = _fp()
        except Exception:
            _lic_email = _lic_type = _lic_expiry = _machine_id = "—"

        try:
            from backend.src.controllers.system_controller import app_version
            _app_ver = app_version()
        except Exception:
            _app_ver = "—"

        _env_label = (
            settings_ctl.get_app_config("account_env") or "demo"
        ).upper()
        _py_ver  = _sys.version.split()[0]
        _os_info = f"{_platform.system()} {_platform.release()} ({_platform.machine()})"

        _sys_header = (
            f"FOREX Trader — Filtered Log Export\n"
            f"{'=' * 80}\n"
            f"Generated  : {now_str}\n"
            f"Period     : last {DAYS} days\n"
            f"Raw lines  : {raw_total:,}  (scanned)\n"
            f"Kept lines : {filtered_total:,}  (errors, warnings, app events)\n"
            f"Dropped    : {raw_total - filtered_total:,}  (DEBUG + polling noise)\n"
            f"\n"
            f"-- User Info --\n"
            f"Email      : {_lic_email}\n"
            f"Machine ID : {_machine_id}\n"
            f"Licence    : {_lic_type}  (expires: {_lic_expiry})\n"
            f"\n"
            f"-- System Info --\n"
            f"App version: {_app_ver}\n"
            f"Environment: {_env_label}\n"
            f"Python     : {_py_ver}\n"
            f"OS         : {_os_info}\n"
            f"{'=' * 80}\n\n"
        )

        # ── Build attachment with hard 25 MB cap ──────────────────────────
        body_text = "\n".join(deduped)
        body_enc  = body_text.encode("utf-8")
        header_enc = _sys_header.encode("utf-8")
        truncated  = False

        if len(header_enc) + len(body_enc) > MAX_BYTES:
            # Keep the most recent lines that fit (newest are at the end)
            available = MAX_BYTES - len(header_enc) - 200  # 200 b margin
            tail_bytes = body_enc[-available:]
            # Realign to the next newline boundary so we don't split a line
            nl_idx = tail_bytes.find(b"\n")
            if nl_idx != -1:
                tail_bytes = tail_bytes[nl_idx + 1:]
            body_text = (
                "[OLDEST ENTRIES TRUNCATED — file exceeded 25 MB limit]\n\n"
                + tail_bytes.decode("utf-8", errors="replace")
            )
            truncated = True

        attachment_bytes = (header_enc + body_text.encode("utf-8"))
        attachment_b64   = _b64.b64encode(attachment_bytes).decode()

        # ── HTML email body ────────────────────────────────────────────────
        _trunc_note = (
            "<p style='color:#f87171;font-size:13px;margin-top:8px;'>"
            "Note: attachment was truncated to fit the 25 MB limit — oldest entries removed."
            "</p>"
        ) if truncated else ""
        size_kb = round(len(attachment_bytes) / 1024)
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#111827;font-family:Arial,sans-serif;color:#e5e7eb;">
<h2 style="color:#f59e0b;margin-bottom:6px;">FOREX Trader &mdash; Filtered Log Export</h2>
<p style="color:#9ca3af;font-size:13px;">
  {now_str} &nbsp;&bull;&nbsp; v{_app_ver}
  &nbsp;&bull;&nbsp; Last {DAYS} days
  &nbsp;&bull;&nbsp; {filtered_total:,} kept / {raw_total:,} scanned
  &nbsp;&bull;&nbsp; {size_kb} KB
</p>
{_trunc_note}
<table style="border-collapse:collapse;margin-top:16px;font-size:13px;">
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Email</td>
      <td style="color:#e5e7eb">{_lic_email}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Machine ID</td>
      <td style="color:#e5e7eb;font-family:monospace">{_machine_id}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Licence</td>
      <td style="color:#e5e7eb">{_lic_type} &mdash; expires {_lic_expiry}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Environment</td>
      <td style="color:#e5e7eb">{_env_label}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">App version</td>
      <td style="color:#e5e7eb">{_app_ver}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Python</td>
      <td style="color:#e5e7eb">{_py_ver}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">OS</td>
      <td style="color:#e5e7eb">{_os_info}</td></tr>
</table>
<p style="color:#d1d5db;font-size:13px;margin-top:16px;">
  Filtered log attached as <strong>{fname}</strong>.<br>
  <span style="color:#6b7280;font-size:12px;">
    Dropped: DEBUG lines, tick polling, candle polling, Telegram getUpdates, position checks.
    Kept: errors, warnings, trades, signals, bridge events, DPM actions, auth events.
  </span>
</p>
</body></html>"""

        # ── Send via Resend with attachment ───────────────────────────────
        ecfg    = _db_ctl.get_email_config()
        api_key = (ecfg.get("resend_api_key") or "").strip()
        if not api_key:
            export_lbl.text = "No Resend API key configured — set it in Settings → Email Reports."
            export_lbl.classes(replace="text-sm mt-1 text-orange-400")
            return

        payload = {
            "from":    "FOREX Trader <onboarding@resend.dev>",
            "to":      [TO],
            "subject": f"FOREX Trader Logs — {now_str}",
            "html":    html,
            "attachments": [
                {"filename": fname, "content": attachment_b64},
            ],
        }

        export_lbl.text = (
            f"Sending {filtered_total:,} filtered lines "
            f"({size_kb} KB) to {TO}..."
        )
        async with _httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if r.status_code in (200, 201):
            export_lbl.text = (
                f"Sent — {filtered_total:,} kept / {raw_total:,} scanned "
                f"({size_kb} KB)."
                + (" [truncated]" if truncated else "")
            )
            export_lbl.classes(replace="text-sm mt-1 text-green-400")
            ui.notify("Log export sent!", type="positive")
        else:
            export_lbl.text = f"Send failed (HTTP {r.status_code}): {r.text[:200]}"
            export_lbl.classes(replace="text-sm mt-1 text-red-400")

    except Exception as exc:
        export_lbl.text = f"Error: {exc}"
        export_lbl.classes(replace="text-sm mt-1 text-red-400")
