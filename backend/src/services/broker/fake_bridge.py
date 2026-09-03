"""FakeMT5Bridge — the offline stand-in for MT5BridgeClient/NativeMT5Bridge.

Implements the exact duck-typed surface the runtime calls on `_bridge`
(pinned by tests/services/broker/test_fake_bridge_surface.py), backed by:

- a deterministic FakeMarket price curve (seeded synthetic stream or a
  scripted scenario — see fake_market.py and tools/debug_scenarios/);
- an internal ledger: account balance, open positions, deal history.
  Fills are exact at the current fake bid/ask (no slippage modelling —
  debug-mode QUESTIONS #4); realised P&L moves the balance; equity tracks
  open profit.

Conventions copied from the real clients, deliberately:
- success dicts carry the same keys mt5_bridge.py returns
  (`ticket`/`fill_price`/... on open, `close_price` on close);
- refusals return `{"error": ...}` — the fake NEVER raises where the real
  client returns a value;
- `inject_error(method, response, count)` makes the fake misbehave on
  demand so rejection paths (the review's C1/C2 class) are testable.

This class contains no network code and can never reach a broker. It is
NOT wired into the runtime: the `_make_bridge` seam edit is Simon-gated
(local-debug-mode 020) and ships separately after his sign-off + demo.
"""
from __future__ import annotations

import time as _time
from typing import Callable, Optional

from backend.src.services.broker.fake_market import TF_SECONDS, FakeMarket
from backend.src.utils.models import CONTRACT_SIZE, Tick

SYMBOL = "XAUUSD"
_LEVERAGE = 500.0


class FakeMT5Bridge:
    def __init__(
        self,
        seed: int = 42,
        scenario: Optional[dict] = None,
        base_ts: Optional[float] = None,
        clock: Callable[[], float] = _time.time,
        starting_balance: float = 1000.0,
    ):
        self._clock = clock
        base = float(base_ts) if base_ts is not None else float(clock())
        self._market = FakeMarket(seed=seed, base_ts=base, scenario=scenario)
        self._balance = float(starting_balance)
        self._positions: dict[int, dict] = {}
        self._deals: list[dict] = [{
            # The initial deposit as MT5 records it (type 2 = balance op) —
            # the frontend's net-deposited / total-P&L maths reads these.
            "ticket": 80000000, "order": 0, "position_id": 0, "entry": 0,
            "symbol": "", "type": 2, "volume": 0.0, "price": 0.0,
            "profit": float(starting_balance), "swap": 0.0, "fee": 0.0,
            "time": base, "comment": "initial deposit",
        }]
        self._next_ticket = 80000001
        self._injected: dict[str, list[dict]] = {}

    # ── Error injection ───────────────────────────────────────────────────

    def inject_error(self, method: str, response: dict, count: int = 1) -> None:
        """Queue `response` to be returned by the next `count` calls of
        `method` instead of the real behaviour."""
        self._injected.setdefault(method, []).extend([response] * count)

    def _pop_injected(self, method: str) -> Optional[dict]:
        queue = self._injected.get(method)
        return queue.pop(0) if queue else None

    # ── Identity / lifecycle ──────────────────────────────────────────────

    @property
    def url(self) -> str:
        return "fake://offline"

    def is_configured(self) -> bool:
        return True

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    # ── Market data ───────────────────────────────────────────────────────

    async def get_tick(self) -> Optional[Tick]:
        return self._market.tick(self._clock())

    async def get_fresh_tick(self) -> Optional[Tick]:
        return self._market.tick(self._clock())

    async def get_tick_at(self, ts: float) -> Optional[dict]:
        tick = self._market.tick(float(ts))
        return {
            "bid": tick.bid, "ask": tick.ask, "spread": tick.spread,
            "spread_points": tick.spread_points, "time": int(ts),
        }

    async def get_candles(self, timeframe: str = "M5", count: int = 200) -> list[dict]:
        tf = TF_SECONDS.get(timeframe)
        if tf is None:
            return []
        return self._market.candles(self._clock(), tf, count)

    async def get_candles_for_symbol(self, symbol: str,
                                     timeframe: str = "M5", count: int = 20) -> list[dict]:
        # One synthetic market: every symbol answers from the same curve.
        return await self.get_candles(timeframe, count)

    async def get_candles_range(self, from_ts: float, to_ts: float,
                                timeframe: str = "M1") -> list[dict]:
        tf = TF_SECONDS.get(timeframe)
        if tf is None:
            return []
        count = max(1, int((float(to_ts) - float(from_ts)) / tf) + 1)
        return self._market.candles(float(to_ts), tf, count)

    async def get_ticks_range(self, from_ts: float, to_ts: float) -> list[dict]:
        return self._market.ticks(float(from_ts), float(to_ts))

    # ── Account / positions ───────────────────────────────────────────────

    def _settle(self) -> None:
        """Emulate MT5's server-side SL/TP execution: a position whose stop
        or target the current price has crossed is closed BY THE BROKER at
        that exact level (QUESTIONS #4: exact fills). The app's monitor loop
        deliberately defers a local SL crossing until the broker's own stop
        fires — without this sweep that deferral would wait forever."""
        tick = self._market.tick(self._clock())
        for ticket, pos in list(self._positions.items()):
            level: Optional[float] = None
            comment = ""
            if pos["type"] == "BUY":
                if pos["sl"] and tick.bid <= pos["sl"]:
                    level, comment = float(pos["sl"]), "[sl]"
                elif pos["tp"] and tick.bid >= pos["tp"]:
                    level, comment = float(pos["tp"]), "[tp]"
            else:
                if pos["sl"] and tick.ask >= pos["sl"]:
                    level, comment = float(pos["sl"]), "[sl]"
                elif pos["tp"] and tick.ask <= pos["tp"]:
                    level, comment = float(pos["tp"]), "[tp]"
            if level is None:
                continue
            if pos["type"] == "BUY":
                profit = round((level - pos["open_price"]) * pos["volume"] * CONTRACT_SIZE, 2)
            else:
                profit = round((pos["open_price"] - level) * pos["volume"] * CONTRACT_SIZE, 2)
            self._balance = round(self._balance + profit, 2)
            self._record_deal(position_id=ticket, entry=1,
                              deal_type=1 if pos["type"] == "BUY" else 0,
                              volume=pos["volume"], price=level, profit=profit,
                              comment=comment)
            del self._positions[ticket]

    def _open_profit(self, pos: dict, tick: Tick) -> float:
        if pos["type"] == "BUY":
            return round((tick.bid - pos["open_price"]) * pos["volume"] * CONTRACT_SIZE, 2)
        return round((pos["open_price"] - tick.ask) * pos["volume"] * CONTRACT_SIZE, 2)

    async def get_health(self) -> dict:
        return {"connected": True, "trade_allowed": True, "source": "fake"}

    async def get_account(self) -> Optional[dict]:
        injected = self._pop_injected("get_account")
        if injected is not None:
            return injected
        self._settle()
        tick = self._market.tick(self._clock())
        open_profit = round(sum(self._open_profit(p, tick) for p in self._positions.values()), 2)
        equity = round(self._balance + open_profit, 2)
        margin = round(sum(p["volume"] * p["open_price"] * CONTRACT_SIZE / _LEVERAGE
                           for p in self._positions.values()), 2)
        return {
            "login": 80000000, "name": "Debug Simulation", "server": "Fake-Demo",
            "currency": "USD", "balance": round(self._balance, 2), "equity": equity,
            "margin": margin, "margin_free": round(equity - margin, 2),
            "margin_level": round(equity / margin * 100.0, 2) if margin else 0.0,
            "leverage": int(_LEVERAGE), "profit": open_profit,
            "trade_mode": 0, "is_demo": True, "trade_allowed": True,
        }

    async def get_positions(self) -> list[dict]:
        injected = self._pop_injected("get_positions")
        if injected is not None:
            return injected  # type: ignore[return-value]
        self._settle()
        tick = self._market.tick(self._clock())
        return [
            {
                "ticket": p["ticket"], "symbol": SYMBOL, "type": p["type"],
                "volume": p["volume"], "open_price": p["open_price"],
                "current_price": tick.bid if p["type"] == "BUY" else tick.ask,
                "sl": p["sl"], "tp": p["tp"],
                "profit": self._open_profit(p, tick), "swap": 0.0,
                "open_time": p["open_time"], "comment": p["comment"],
            }
            for p in self._positions.values()
        ]

    # ── Orders ────────────────────────────────────────────────────────────

    def _record_deal(self, *, position_id: int, entry: int, deal_type: int,
                     volume: float, price: float, profit: float, comment: str) -> None:
        self._deals.append({
            "ticket": self._next_ticket, "order": self._next_ticket,
            "position_id": position_id, "entry": entry, "symbol": SYMBOL,
            "type": deal_type, "volume": volume, "price": price,
            "profit": profit, "swap": 0.0, "fee": 0.0,
            "time": self._clock(), "comment": comment,
        })
        self._next_ticket += 1

    async def place_order(self, direction: str, lots: float,
                          sl: Optional[float], tp: Optional[float],
                          comment: str = "") -> dict:
        injected = self._pop_injected("place_order")
        if injected is not None:
            return injected
        tick = self._market.tick(self._clock())
        direction = direction.upper()
        fill = tick.ask if direction == "BUY" else tick.bid
        ticket = self._next_ticket
        self._next_ticket += 1
        self._positions[ticket] = {
            "ticket": ticket, "type": direction, "volume": round(float(lots), 2),
            "open_price": fill, "sl": sl, "tp": tp,
            "open_time": self._clock(), "comment": comment or "ForexTrader",
        }
        self._record_deal(position_id=ticket, entry=0,
                          deal_type=0 if direction == "BUY" else 1,
                          volume=round(float(lots), 2), price=fill, profit=0.0,
                          comment=comment or "ForexTrader")
        return {
            "success": True, "ticket": ticket, "fill_price": fill,
            "volume": round(float(lots), 2), "direction": direction,
            "sl": sl, "tp": tp,
        }

    def _close_lots(self, pos: dict, lots: float, comment: str) -> dict:
        tick = self._market.tick(self._clock())
        close_price = tick.bid if pos["type"] == "BUY" else tick.ask
        if pos["type"] == "BUY":
            profit = round((close_price - pos["open_price"]) * lots * CONTRACT_SIZE, 2)
        else:
            profit = round((pos["open_price"] - close_price) * lots * CONTRACT_SIZE, 2)
        self._balance = round(self._balance + profit, 2)
        self._record_deal(position_id=pos["ticket"], entry=1,
                          deal_type=1 if pos["type"] == "BUY" else 0,
                          volume=lots, price=close_price, profit=profit,
                          comment=comment)
        return {"close_price": close_price, "profit": profit}

    async def close_position(self, ticket: int) -> dict:
        injected = self._pop_injected("close_position")
        if injected is not None:
            return injected
        self._settle()
        pos = self._positions.get(int(ticket))
        if pos is None:
            return {"error": f"Position {ticket} not found"}
        done = self._close_lots(pos, pos["volume"], "close")
        del self._positions[int(ticket)]
        return {"success": True, "ticket": int(ticket), "close_price": done["close_price"]}

    async def partial_close(self, ticket: int, lots: float) -> dict:
        injected = self._pop_injected("partial_close")
        if injected is not None:
            return injected
        self._settle()
        pos = self._positions.get(int(ticket))
        if pos is None:
            return {"error": f"Position {ticket} not found"}
        lots = max(0.01, min(pos["volume"], round(float(lots), 2)))
        done = self._close_lots(pos, lots, "partial")
        remaining = round(pos["volume"] - lots, 2)
        if remaining <= 0:
            del self._positions[int(ticket)]
        else:
            pos["volume"] = remaining
        return {"success": True, "ticket": int(ticket), "lots_closed": lots,
                "close_price": done["close_price"], "remaining": remaining}

    async def modify_order(self, ticket: int, sl: Optional[float], tp: Optional[float]) -> dict:
        injected = self._pop_injected("modify_order")
        if injected is not None:
            return injected
        pos = self._positions.get(int(ticket))
        if pos is None:
            return {"error": f"Position {ticket} not found"}
        if sl:
            pos["sl"] = sl
        if tp:
            pos["tp"] = tp
        return {"success": True, "ticket": int(ticket)}

    # ── History ───────────────────────────────────────────────────────────

    async def get_deal_history(self, days: int = 7) -> list[dict]:
        cutoff = self._clock() - float(days) * 86400.0
        return [d for d in self._deals if d["time"] >= cutoff or d["type"] == 2]

    async def get_position_history(self, ticket: int) -> list[dict]:
        return [d for d in self._deals if d["position_id"] == int(ticket)]

    # ── Passive stubs ─────────────────────────────────────────────────────

    async def send_credentials(self, login: int, password: str, server: str) -> dict:
        return {"status": "connected", "source": "fake"}

    async def reconnect(self) -> dict:
        return {"status": "connected", "source": "fake"}

    async def enable_autotrading(self) -> dict:
        return {"enabled": True, "source": "fake"}
