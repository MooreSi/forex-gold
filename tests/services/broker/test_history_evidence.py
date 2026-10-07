"""Compile the read-only history function with a fake MT5 namespace; no SDK/orders."""
import ast
from pathlib import Path
from types import SimpleNamespace
import logging


def test_history_retains_commission_and_millisecond_fill_timestamp():
    tree = ast.parse(Path("mt5_bridge.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_get_history_by_position")
    deal = SimpleNamespace(ticket=1, order=2, position_id=3, entry=0, symbol="XAUUSD", type=0,
        volume=.01, price=100, profit=0, swap=0, fee=0, commission=-.5,
        time=100, time_msc=100123, comment="entry")
    calls = []
    def history(**kw):
        calls.append(kw)
        return [deal]
    scope = {"mt5": SimpleNamespace(history_deals_get=history), "_ensure_connected": lambda: True,
             "log": logging.getLogger("test")}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "mt5_bridge.py", "exec"), scope)
    rows = scope["_get_history_by_position"](3)
    assert rows[0]["commission"] == -.5
    assert rows[0]["time_msc"] == 100123
    assert calls == [{"position": 3}]
