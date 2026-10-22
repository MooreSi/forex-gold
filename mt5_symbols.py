"""The broker's own name for a symbol the app asks for. Read-only.

Standard library only, like mt5_ticks.py: mt5_bridge.py imports this, and on
macOS the bridge runs under Wine's Python, which has none of the app's
dependencies.

Why it exists (docs/todo/broker/010, 2026-10-22): the cross-asset sweep asks
for seven peers by the names the owner's broker uses (`SP500`, `USDX`,
`USOUSD`, ...). Customer EV-156MMIO7U6F's broker served gold and none of
them, and the sweep asked again every minute. Brokers rename symbols in three
ways, and each is undone here:

- an account-type affix: `XAGUSD.a`, `XAGUSD.pro`, `XAGUSDm`, `XAGUSD+`,
  `#XAGUSD`, `US500.cash`;
- a different name for the same market: `US500` / `SPX500`, `DXY`, `USOIL` /
  `XTIUSD`, `SILVER`, `GOLD`;
- a currency pair under any name, found by its base and profit currencies.

A name the broker has exactly is always returned unchanged, so a broker that
already worked reads what it read before. A dated future (`DX-DEC26`) is never
matched: it expires, and its price is not the spot one the features assume.

The bridge's trading symbol is never passed through here (`for_read`). Its
contract size, lots and stops differ between a broker's gold symbols, and
that is the order path: phase 2 of the spec, with a demo session.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger("mt5_bridge")

# Other names brokers use for the same market. The app's own name is tried
# first and need not be listed.
ALIASES: dict[str, tuple[str, ...]] = {
    "XAUUSD": ("GOLD",),
    "XAGUSD": ("SILVER",),
    "XPTUSD": ("PLATINUM",),
    "USDX":   ("DXY", "DX", "USDIDX", "USDINDEX", "DOLLARINDEX"),
    "SP500":  ("US500", "SPX500", "USSPX500", "SPX"),
    "VIX":    ("VOLX",),
    "USOUSD": ("USOIL", "XTIUSD", "WTI", "WTIUSD", "USCRUDE", "CRUDEOIL"),
}

# What may follow the market's name: a separator and a short tag (`.a`,
# `_i`, `.cash`), a short lower-case tag (`m`, `c`, `pro`), `Cash`, or a
# lone `+`/`#`/`!`. Upper-case letters or digits straight after the name are
# a different market (`US5000`, `XAUUSDX`), and `-` is how futures are dated.
_SUFFIX = re.compile(r"(?:[._][A-Za-z0-9]{1,8}|[a-z]{1,4}|Cash|CASH|[+#!])?")
_PREFIX = re.compile(r"[#_.]?")
_DATED = re.compile(r"(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}"
                    r"|-", re.IGNORECASE)

_lists: dict[str, list] = {}
_resolved: dict[tuple[str, str], str] = {}


def reset_cache() -> None:
    _lists.clear()
    _resolved.clear()


def _affix(name: str, market: str) -> Optional[tuple[str, str]]:
    """(prefix, suffix) when `name` is `market` with an affix, else None."""
    prefix = _PREFIX.match(name).group(0)
    core = name[len(prefix):]
    if not core.upper().startswith(market):
        return None
    rest = core[len(market):]
    if _SUFFIX.fullmatch(rest) is None:
        return None
    return prefix, rest


def _is_pair(market: str) -> bool:
    return len(market) == 6 and market.isalpha()


def _by_name(market: str, symbols: list) -> list[tuple[Any, tuple[str, str]]]:
    out = []
    for s in symbols:
        if _DATED.search(s.name):
            continue
        # `Silver_EU` passes on its name; the broker says it is quoted in
        # euros. Believed whenever the broker says anything.
        profit = getattr(s, "currency_profit", "")
        if _is_pair(market) and profit and profit != market[3:]:
            continue
        for alias in (market,) + ALIASES.get(market, ()):
            affix = _affix(s.name, alias)
            if affix is not None:
                out.append((s, affix))
                break
    return out


def _by_currencies(market: str, symbols: list) -> list[tuple[Any, tuple[str, str]]]:
    if not _is_pair(market):
        return []
    base, profit = market[:3], market[3:]
    return [(s, ("", "")) for s in symbols
            if not _DATED.search(s.name)
            and getattr(s, "currency_base", "") == base
            and getattr(s, "currency_profit", "") == profit]


def match(market: str, symbols: list) -> Optional[str]:
    """The broker's name for `market` among `symbols` (MT5 SymbolInfo-shaped:
    `name`, `currency_base`, `currency_profit`, `visible`), or None.

    Several candidates are ranked: the exact name, then the affix the
    broker's gold carries (an account's symbols share one), then one already
    in Market Watch, then the shortest name."""
    market = market.upper()
    found = _by_name(market, symbols) or _by_currencies(market, symbols)
    if not found:
        return None
    gold = sorted(_by_name("XAUUSD", symbols), key=lambda c: len(c[0].name))
    gold_affix = gold[0][1] if gold else None

    def rank(c):
        s, affix = c
        return (s.name != market, affix != gold_affix,
                not getattr(s, "visible", False), len(s.name), s.name)
    return min(found, key=rank)[0].name


def resolve(mt5: Any, market: str) -> str:
    """The broker's name for `market`, or `market` itself when the broker has
    that name or nothing like it. Cached per broker server, misses included,
    so the symbol list is read once a session."""
    try:
        info = mt5.account_info()
        server = getattr(info, "server", "") or ""
        key = (server, market)
        if key in _resolved:
            return _resolved[key]
        if mt5.symbol_info(market) is not None:
            _resolved[key] = market
            return market
        if server not in _lists:
            listed = mt5.symbols_get()
            if listed is None:
                return market
            _lists[server] = list(listed)
        name = match(market, _lists[server])
        if name is None:
            log.info("symbols: %s is not offered by this broker", market)
        elif name != market:
            log.info("symbols: %s is %s on this broker", market, name)
        _resolved[key] = name or market
        return _resolved[key]
    except Exception as e:
        log.warning("symbols: could not look up %s: %s", market, e)
        return market


def for_read(mt5: Any, market: str, trading_symbol: str) -> str:
    """`resolve`, except the bridge's trading symbol, which is never remapped."""
    return market if market == trading_symbol else resolve(mt5, market)


def show(mt5: Any, name: str) -> bool:
    """Add a symbol the broker has to Market Watch, which MT5 may need before
    it quotes it. False, and nothing selected, when the broker lacks it."""
    try:
        if mt5.symbol_info(name) is None:
            return False
        return bool(mt5.symbol_select(name, True))
    except Exception:
        return False
