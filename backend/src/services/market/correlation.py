"""Cross-asset correlation, and what it means for exposure.

Section 5.9 of `docs/todo/reversal-engine/200`.

Everything this app does is a single-instrument bet on gold, and
`_MAX_OPEN_SIGNALS = 6` counts SIGNALS rather than exposure: six open XAUUSD
longs are one position of six times the size, and the cap cannot tell.

Multi-symbol TRADING is a much larger piece of work than this module -- the
bridge binds one symbol at module level (`MT5_SYMBOL`), and so do the EA and
the trade schema. What is available today with no new plumbing is
multi-symbol market DATA: `get_candles_for_symbol` is wired from
`mt5_bridge` through to `runtime`. That is enough for cross-asset context
and for an exposure number that knows two positions can be one bet.

Everything here is pure. It measures; it sizes nothing and caps nothing.
"""
from __future__ import annotations

import math
from typing import Optional, Sequence

Return = tuple[float, float]     # (ts, pct return)


def returns(candles: Sequence[dict]) -> list[Return]:
    """Per-bar percentage returns, tagged with the bar's timestamp.

    A zero or missing close breaks the chain rather than dividing by it:
    MT5 reports 0.0 for no quote, and a return computed against it would be
    the largest move in the series on every occasion it appeared.
    """
    out: list[Return] = []
    prev: Optional[float] = None
    for c in candles or ():
        try:
            close = float(c.get("close", c.get("c", 0)) or 0.0)
            ts = float(c.get("ts") or c.get("time") or 0.0)
        except (TypeError, ValueError):
            prev = None
            continue
        if close <= 0:
            prev = None
            continue
        if prev is not None and prev > 0:
            out.append((ts, (close - prev) / prev))
        prev = close
    return out


def align(a: Sequence[Return], b: Sequence[Return]) -> tuple[list, list]:
    """Paired returns on timestamps present in BOTH series.

    Gold trades hours that equities do not. Comparing unaligned series
    pairs Monday's gold with Friday's S&P and calls the result a
    correlation.
    """
    by_ts = {ts: v for ts, v in b}
    xs, ys = [], []
    for ts, v in a:
        if ts in by_ts:
            xs.append(v)
            ys.append(by_ts[ts])
    return xs, ys


def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Correlation coefficient, or None when it is undefined.

    None rather than 0.0 for a flat series or too few points. Zero is a
    claim -- "these move independently" -- and a series that never moved
    supports no claim at all.
    """
    n = min(len(xs), len(ys))
    if n < 2:
        return None
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    sxx = sum((xs[i] - mx) ** 2 for i in range(n))
    syy = sum((ys[i] - my) ** 2 for i in range(n))
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def correlation_matrix(candles_by_symbol: dict) -> dict:
    """`{(a, b): rho}` for every measurable pair, a < b."""
    rets = {sym: returns(c) for sym, c in (candles_by_symbol or {}).items()}
    out: dict = {}
    symbols = sorted(rets)
    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            xs, ys = align(rets[a], rets[b])
            rho = pearson(xs, ys)
            if rho is not None:
                out[(a, b)] = rho
    return out


def effective_exposure(signed_lots: dict, correlations: dict) -> float:
    """One exposure number for a book of correlated positions.

    The portfolio standard deviation with every instrument treated as
    having unit volatility: `sqrt(sum_i sum_j w_i w_j rho_ij)`. Adding lots
    across instruments as though they were one position overstates risk;
    ignoring correlation entirely understates it. Neither is the number a
    cap should be applied to.

    **An unmeasured pair is assumed perfectly correlated**, not independent.
    Assuming independence for a pair nobody measured is how a risk system
    discovers, in a drawdown, that everything it held was the same trade.
    """
    symbols = list(signed_lots)
    total = 0.0
    for a in symbols:
        for b in symbols:
            wa, wb = float(signed_lots[a]), float(signed_lots[b])
            if a == b:
                rho = 1.0
            else:
                key = (a, b) if (a, b) in correlations else (b, a)
                rho = correlations.get(key, 1.0)
            total += wa * wb * rho
    return math.sqrt(max(0.0, total))


PEER_SYMBOLS = ("XAGUSD", "EURUSD", "USDJPY")


async def snapshot(bridge, symbols=PEER_SYMBOLS, base: str = "XAUUSD",
                   timeframe: str = "H1", count: int = 200) -> dict:
    """Correlations between `base` and each peer over recent candles.

    Never raises: a peer the broker does not offer, or a bridge that is
    down, costs its own row and nothing else. A missing symbol is simply
    absent from the result rather than present with a fabricated zero.
    """
    series: dict = {}
    for sym in (base, *symbols):
        try:
            candles = await bridge.get_candles_for_symbol(sym, timeframe, count)
        except Exception:                         # noqa: BLE001
            continue
        if candles:
            series[sym] = candles
    matrix = correlation_matrix(series)
    return {"symbols": sorted(series),
            "correlations": {f"{a}/{b}": round(v, 4)
                             for (a, b), v in matrix.items()}}
