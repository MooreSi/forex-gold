"""`GET /api/trading/signals` on a Mac that trades through the VPS.

The service half is tests/core/test_signals_follow_the_trading_node.py. This is
the boundary: an unreachable trader is a 503 that says so (the same as the Fill
cost and GEX cards), and the per-row `node` survives `SignalOut`, because the
table uses it to keep the editor off rows that live in the VPS's database.
"""
from __future__ import annotations

class TestTheRoute:
    def test_an_unreachable_trader_is_a_503_with_the_reason(self, make_client, monkeypatch):
        from backend.src.api.routers import trading as trading_router

        async def _down(engine, status=None):
            raise trading_router.trading_ctl.RemoteControlFailed(
                "The trading node could not be reached (not connected to VPS).")
        monkeypatch.setattr(trading_router.trading_ctl, "get_signals", _down)

        resp = make_client().get("/api/trading/signals")

        assert resp.status_code == 503
        assert "could not be reached" in resp.text

    def test_the_node_field_reaches_the_browser(self, make_client, monkeypatch):
        from backend.src.api.routers import trading as trading_router

        async def _rows(engine, status=None):
            return [{"signal_id": "vps-today", "direction": "BUY", "node": "remote"}]
        monkeypatch.setattr(trading_router.trading_ctl, "get_signals", _rows)

        body = make_client().get("/api/trading/signals").json()

        assert body[0]["node"] == "remote"
