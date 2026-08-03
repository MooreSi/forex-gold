"""Formatting helpers shared across the trading page sections.

Small, pure, and used by more than one section -- which is exactly why
they live here rather than in whichever section happened to need them
first.
"""
from datetime import datetime, timezone
from nicegui import ui


def _uk(ts) -> str:
    """Format an MT5 broker timestamp for display.
    MT5 timestamps are UTC+3 encoded as Unix epoch; treating as UTC gives broker time."""
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        # ISO string (e.g. from Telegram message_ts)
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%m-%d %H:%M")
        except Exception:
            return str(ts)[:16]
    except Exception:
        return str(ts)[:16]
def _pnl_colour(v: float) -> str:
    if v > 0:  return "text-green-400"
    if v < 0:  return "text-red-400"
    return "text-gray-400"
def _pnl_bg(v: float) -> str:
    if v > 0:  return "bg-green-900"
    if v < 0:  return "bg-red-900"
    return "bg-gray-800"
def _tp_progress(triggered: set[int], trade: dict) -> None:
    """Render TP1–TP5 chips with actual price values."""
    with ui.column().classes("gap-0.5"):
        for n in range(1, 9):
            tp_val = trade.get(f"tp{n}")
            if not tp_val:
                continue
            hit = n in triggered
            chip_col = "bg-green-500" if hit else "bg-gray-600"
            val_col  = "text-green-300" if hit else "text-gray-400"
            with ui.row().classes("items-center gap-1"):
                ui.label(f"TP{n}").classes(
                    f"text-xs px-1.5 py-0.5 rounded font-semibold text-white {chip_col}"
                )
                ui.label(f"${float(tp_val):.2f}").classes(
                    f"text-xs font-mono {val_col}"
                )
def _stat_cell(label: str, value: str, value_cls: str = "text-gray-200") -> None:
    with ui.column().classes("gap-0"):
        ui.label(label).classes("text-xs text-gray-500 tracking-wider font-medium")
        ui.label(value).classes(f"text-sm font-mono font-semibold {value_cls}")
