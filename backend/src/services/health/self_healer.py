"""
Self-healing monitor for the FOREX Trader app.

Runs as a background task. Every 90 seconds it scans recent log lines for
known recoverable error patterns. When a healable condition is confirmed it
takes the minimum necessary action, then sends a Telegram message and email
so the user knows what happened.

Healable conditions:
  - Bridge offline / connection-refused repeated errors → restart bridge process
  - Telegram repeated drops → force reconnect
  - Position monitor loop crash → restart monitor

Only fires if the same error pattern appears >= THRESHOLD times in the
WINDOW_SECONDS rolling window, to avoid reacting to transient one-off errors
that the existing watchdogs would already handle.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.src.runtime import TradingRuntime as Engine

_log = logging.getLogger("forex_trader")

# ── Tunables ──────────────────────────────────────────────────────────────────
_POLL_INTERVAL  = 90      # seconds between scans
_WINDOW_SECONDS = 300     # look back 5 min when counting occurrences
_THRESHOLD      = 3       # occurrences in window before acting
_COOLDOWN       = 600     # minimum seconds between heal actions for same condition
_BRIDGE_STARTUP_GRACE = 330   # seconds after engine start to ignore bridge_offline noise;
                              # must exceed _WINDOW_SECONDS (300s) or the lookback scan can
                              # still see the tail of the startup burst after grace lifts

_TO_EMAIL = "simon.moore@outlook.com"


# ── Patterns ─────────────────────────────────────────────────────────────────
# Each entry: (condition_key, compiled_regex_on_log_line)
_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "bridge_offline",
        re.compile(r"(Connection refused.*9000|bridge.*offline|bridge.*not.*respond)", re.I),
    ),
    (
        "telegram_dropped",
        re.compile(r"(Watchdog.*client dropped|telethon.*disconnect|ConnectionResetError)", re.I),
    ),
    (
        "monitor_crash",
        re.compile(r"(Exception in monitor loop|Error in position monitor|monitor.*crash)", re.I),
    ),
]


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# Log volume varies, but this app's chattiest bursts (loop_monitor task-list
# dumps, httpx per-request lines) still fit a 5-minute window in well under
# 1MB even on a noisy day. The file itself is daily-rotating and grows to
# tens of MB by evening, so seeking to a tail chunk instead of scanning from
# byte 0 keeps this bounded regardless of how large the day's log has gotten.
_TAIL_CHUNK_BYTES = 2 * 1024 * 1024


def _read_recent_log_lines(window_seconds: int) -> list[tuple[float, str]]:
    """Return [(timestamp, line), ...] for log lines within the window.

    Reads only a tail chunk of the log file rather than scanning from the
    start — this file grows all day (rotates at midnight, not by size), and
    a full linear scan every _POLL_INTERVAL seconds becomes a multi-second
    (worst case much longer, under disk/memory pressure) synchronous block
    on the event loop as the file grows, since this runs unwrapped inside an
    async task. A tail read comfortably covers _WINDOW_SECONDS regardless of
    total file size.
    """
    from backend.src.config import DATA_DIR
    from datetime import datetime
    log_base = Path(DATA_DIR) / "forex_trader.log"
    if not log_base.exists():
        return []

    cutoff = time.time() - window_seconds
    result: list[tuple[float, str]] = []

    try:
        with log_base.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_CHUNK_BYTES))
            chunk = fh.read().decode("utf-8", errors="replace")

        lines = chunk.splitlines()
        # The tail read may start mid-line or mid-window; if even the first
        # line is already within the window, the chunk might not reach far
        # enough back — fall back to the whole file in that rare case.
        if lines:
            first_ts = None
            for line in lines[:5]:
                m = _TS_RE.match(line)
                if m:
                    try:
                        first_ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                    except Exception:
                        pass
                    break
            if first_ts is not None and first_ts >= cutoff and size > _TAIL_CHUNK_BYTES:
                with log_base.open("r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()

        for line in lines:
            line = line.rstrip()
            if not line:
                continue
            m = _TS_RE.match(line)
            if m:
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                    if ts >= cutoff:
                        result.append((ts, line))
                except Exception:
                    pass
    except Exception:
        pass

    return result


async def _send_heal_notification(condition: str, action: str) -> None:
    """Send Telegram alert + email to inform the user of a self-heal action."""
    msg = (
        f"Self-heal: {condition} detected.\n"
        f"Action taken: {action}\n"
        f"Time: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
    )

    try:
        from backend.src.services.telegram import alerts as telegram_alerts
        await telegram_alerts.send_message(msg)
    except Exception as e:
        _log.warning("[SelfHealer] Telegram notification failed: %s", e)

    try:
        from backend.src.services.notifications import email_service
        html = (
            "<h2>FOREX Trader — Self-Heal Event</h2>"
            f"<p><b>Condition:</b> {condition}</p>"
            f"<p><b>Action:</b> {action}</p>"
            f"<p><b>Time (UTC):</b> {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}</p>"
            "<p>This is an automated message from the FOREX Trader self-healing monitor.</p>"
        )
        ok, err = await email_service.send_email(
            subject=f"FOREX Trader — Self-Heal: {condition}",
            html_body=html,
        )
        if not ok:
            _log.warning("[SelfHealer] Email notification failed: %s", err)
    except Exception as e:
        _log.warning("[SelfHealer] Email send error: %s", e)


class SelfHealer:
    def __init__(self, engine: "Engine") -> None:
        self._engine   = engine
        self._last_heal: dict[str, float] = {}  # condition → last heal timestamp
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        _log.info("[SelfHealer] Started — polling every %ds", _POLL_INTERVAL)

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        await asyncio.sleep(120)  # let the app settle on startup before first check
        while True:
            try:
                await self._scan()
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log.warning("[SelfHealer] Scan error: %s", e)
            await asyncio.sleep(_POLL_INTERVAL)

    async def _scan(self) -> None:
        lines = await asyncio.to_thread(_read_recent_log_lines, _WINDOW_SECONDS)
        if not lines:
            return

        counts: dict[str, int] = {}
        for _ts, line in lines:
            for key, pattern in _PATTERNS:
                if pattern.search(line):
                    counts[key] = counts.get(key, 0) + 1

        for condition, count in counts.items():
            if count < _THRESHOLD:
                continue

            if condition == "bridge_offline":
                started_at = getattr(self._engine, "_started_at", None)
                if started_at is not None and time.monotonic() - started_at < _BRIDGE_STARTUP_GRACE:
                    # A full VPS/OS reboot needs MT5 to cold-start and log into
                    # the broker before the bridge responds — observed taking
                    # up to ~150s. Without this, every reboot's normal warmup
                    # gets misread as a real outage and triggers a redundant
                    # restart + alert email on top of the bridge watchdog's own.
                    _log.info(
                        "[SelfHealer] bridge_offline pattern seen during startup "
                        "grace window (%d occurrences) — treating as normal MT5 "
                        "cold-start warmup, not healing",
                        count,
                    )
                    continue

            now = time.time()
            last = self._last_heal.get(condition, 0.0)
            if now - last < _COOLDOWN:
                continue  # already acted recently — don't spam

            _log.warning(
                "[SelfHealer] Condition '%s' triggered (%d occurrences in %ds window) — healing",
                condition, count, _WINDOW_SECONDS,
            )
            self._last_heal[condition] = now
            await self._heal(condition)

    async def _heal(self, condition: str) -> None:
        if condition == "bridge_offline":
            try:
                launched = await self._engine.start_bridge_process()
                if launched:
                    action = "Bridge process restarted."
                else:
                    import sys as _sys
                    hint = "check the MT5 terminal" if _sys.platform == "win32" else "check Wine/CrossOver"
                    action = f"Bridge restart attempted but launch failed — {hint}."
            except Exception as e:
                action = f"Bridge restart failed: {e}"

        elif condition == "telegram_dropped":
            try:
                from backend.src.services.telegram import alerts as _ta
                # Access the reader through the engine if available
                reader = getattr(self._engine, "_tg_reader", None)
                if reader:
                    result = await reader.reconnect()
                    action = "Telegram client force-reconnected." if result.get("ok") else f"Reconnect attempted: {result.get('error', 'unknown')}"
                else:
                    action = "Telegram reader not accessible from self-healer."
            except Exception as e:
                action = f"Telegram reconnect failed: {e}"

        elif condition == "monitor_crash":
            # The monitor loop crash is fatal — recommend restart via Telegram
            action = "Position monitor crash detected. Recommend /restart_app to recover."

        else:
            action = f"Unknown condition '{condition}' — no action taken."

        _log.info("[SelfHealer] Action: %s", action)
        await _send_heal_notification(condition, action)
