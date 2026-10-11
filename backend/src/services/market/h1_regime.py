"""H1 regime: trend or range, by how efficiently price travelled.

From docs/todo/014. The efficiency ratio over the last `hours` closed H1
bars is |net move| / the sum of every bar's |close - open|. On 2026-10-05
(choppy) and 2026-10-06 (a rally) the M5 classifier Auto had used
(dpm_engine.detect_regime) said "trending" ~60% of both days; the efficiency
ratio over the day was 0.05 against 0.21.

Pure: bars in, a verdict out. Read by Auto when
`auto_template_h1_regime_enabled` is on (positions/core_auto_template.py)
and by tools/auto_template_study.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


@dataclass(frozen=True)
class RegimeRead:
    regime: str      # "trend" | "range"
    direction: str   # "up" | "down" | "flat"
    er: float


def read(bars: Sequence[dict], hours: int, threshold: float) -> Optional[RegimeRead]:
    """The regime over the last `hours` closed H1 bars, or None with fewer."""
    if hours <= 0 or len(bars) < hours:
        return None
    window = bars[-hours:]
    net = float(window[-1]["close"]) - float(window[0]["open"])
    path = sum(abs(float(b["close"]) - float(b["open"])) for b in window)
    er = round(abs(net) / path, 4) if path > 0 else 0.0
    direction = "up" if net > 0 else "down" if net < 0 else "flat"
    regime = "trend" if er >= threshold and direction != "flat" else "range"
    return RegimeRead(regime, direction, er)


def side(r: Optional[RegimeRead], trade_direction: str) -> str:
    """"with" / "against" the trend, or "flat" in a range or when unknown."""
    if r is None or r.regime != "trend":
        return "flat"
    buy = str(trade_direction).upper() == "BUY"
    return "with" if (r.direction == "up") == buy else "against"
