"""Both deal-history reads carry MT5's own DEAL_REASON.

The Trade History table labelled every blank-comment close "Manual". The EA's
own partial and final TP closes leave a blank comment, and so does a close made
by hand in the terminal; only MT5's `reason` (3 = an expert, 0 = the terminal)
tells them apart. Compiled with a fake MT5 namespace: no SDK, no orders.
"""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest


def _deal(**over):
    row = dict(ticket=1, order=2, position_id=3, entry=1, symbol="XAUUSD", type=1,
               volume=.01, price=100, profit=0, swap=0, fee=0, commission=0,
               time=100, time_msc=100000, comment="", reason=3)
    row.update(over)
    return SimpleNamespace(**row)


def _compile(name, deals):
    tree = ast.parse(Path("mt5_bridge.py").read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    mt5 = SimpleNamespace(history_deals_get=lambda *a, **k: deals,
                          terminal_info=lambda: None)
    scope = {"mt5": mt5, "_ensure_connected": lambda: True, "SYMBOL": "XAUUSD",
             "log": logging.getLogger("test")}
    exec("from datetime import datetime, timezone, timedelta\nimport time", scope)
    exec(compile(ast.Module(body=[node], type_ignores=[]), "mt5_bridge.py", "exec"), scope)
    return scope[name]


@pytest.mark.parametrize("name,args", [("_get_history_by_position", (3,)),
                                       ("_get_history", (7,))])
def test_each_history_read_reports_the_deal_reason(name, args):
    rows = _compile(name, [_deal(reason=3), _deal(ticket=9, reason=0)])(*args)
    assert [r["reason"] for r in rows] == [3, 0]


@pytest.mark.parametrize("name,args", [("_get_history_by_position", (3,)),
                                       ("_get_history", (7,))])
def test_a_deal_without_the_field_reports_none_rather_than_failing(name, args):
    deal = _deal()
    del deal.reason
    rows = _compile(name, [deal])(*args)
    assert rows[0]["reason"] is None
