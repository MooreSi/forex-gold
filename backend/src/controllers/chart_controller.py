"""Chart page's API."""
from __future__ import annotations

from typing import Any

from backend.src.services.cluster import node as _node
from backend.src.services.positions import core_indicators as _ind
from backend.src.services.reversal_engine import ict_patterns as _ict
from backend.src.services.risk import settings as _risk

__all__ = ["get_active_trader", "get_risk_settings", "get_open_trades"]


def get_active_trader() -> str:
    return _node.get_active_trader()


def get_risk_settings() -> dict:
    return _risk.get()


async def get_open_trades(engine: Any) -> list[dict]:
    """The page polls this from a ui.timer -- the engine read is dispatched
    off the loop by the positions service."""
    from backend.src.services.trading import engine_reads as _reads
    return await _reads.open_trades(engine)


def ema_series(*args, **kwargs):
    """Exponential moving average over a candle series. Pure maths, no I/O."""
    return _ind.ema_series(*args, **kwargs)


def rsi_series(*args, **kwargs):
    """RSI over a candle series. Pure maths, no I/O."""
    return _ind.rsi_series(*args, **kwargs)


def detect_fvgs(*args, **kwargs):
    """Fair-value gaps in a candle series. Pure analysis, drawn as overlays."""
    return _ict.detect_fvgs(*args, **kwargs)


def select_display_fvgs(*args, **kwargs):
    """Thin the detected gaps down to what is worth drawing on the chart."""
    return _ict.select_display_fvgs(*args, **kwargs)
