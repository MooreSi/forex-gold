"""The trade-open alert states the take-profits and the risk basis (2026-09-28).

Reported live on ticket 2103198838 ("30 TP1 SL50 and Trail", Gold Diggers
VIP): the message carried SL but no TP, so there was no telling whether the
template's ladder was in force, and nothing said the lot came from a risk
percentage.

* The row's own tp1..tp8 are printed as before whenever it has them.
* An EA Template row with none gets the template's ladder as distances --
  what the template tells the EA -- labelled as such, never invented prices.
* A risk-% sized trade says so; a fixed-lot trade says nothing extra. The
  line states the BASIS only: the channel lot multiplier, the signal-age
  shrink and the contradiction policy scale some routes' lots and not others,
  so the lot on the line above is the figure that was traded.

Formatting only. Nothing here reaches a broker.
"""
import pytest

from backend.src.services.broker import ea_templates
from backend.src.services.telegram import alerts as ta

_TEMPLATE = {
    "name": "30 TP1 SL50 and Trail", "mode": "single", "anchors": 1,
    "pendings": 0, "lot_anchor": 0.1, "risk_pct": 0.0,
    "tp1_pips": 40.0, "tp2_pips": 50.0, "tp3_pips": 0.0,
    "tp1_pct": 55.0, "tp2_pct": 10.0,
}


def _trade(**over):
    row = {
        "trade_id": "t1", "direction": "BUY", "mt5_ticket": 2103198838,
        "entry_price": 4151.03, "entry_low": 4148.45, "entry_high": 4150.45,
        "lot_size": 0.03, "stop_loss": 4145.91,
        "strategy": "template:30 TP1 SL50 and Trail",
        "tg_source": "Gold Diggers VIP", "managed_by": "ea",
    }
    row.update(over)
    return row


@pytest.fixture
def settings(monkeypatch):
    rs = {"strategy_lot_size": 0, "risk_per_trade_pct": 1.0,
          "global_sizing_override": 0, "dpm_enabled": 0}
    template = dict(_TEMPLATE)
    monkeypatch.setattr(ta.db_module, "get_risk_settings", lambda: rs)
    monkeypatch.setattr(ta.db_module, "get_effective_strategy",
                        lambda _rs: (None, False))
    monkeypatch.setattr(ta.db_module, "get_app_config", lambda _k: None)
    monkeypatch.setattr(ea_templates, "get_ea_template",
                        lambda name: template if name == template["name"] else None)
    return {"rs": rs, "template": template}


class TestTakeProfits:
    def test_the_rows_own_levels_are_printed(self, settings):
        msg = ta.fmt_trade_open(_trade(tp1=4155.03, tp2=4156.03), None, {})
        assert "TP1: 4155.03" in msg
        assert "TP2: 4156.03" in msg
        assert "managed by the EA" not in msg

    def test_a_template_row_without_levels_shows_the_templates_ladder(self, settings):
        msg = ta.fmt_trade_open(_trade(), None, {})
        assert "TP1: +40 pips (55%)" in msg
        assert "TP2: +50 pips (10%)" in msg
        assert "TP3" not in msg
        assert "managed by the EA" in msg

    def test_a_template_with_no_ladder_says_there_is_none(self, settings):
        for n in (1, 2):
            settings["template"][f"tp{n}_pips"] = 0.0
        msg = ta.fmt_trade_open(_trade(), None, {})
        assert "TP: none set by the template" in msg

    def test_a_non_template_row_without_levels_adds_nothing(self, settings):
        msg = ta.fmt_trade_open(_trade(strategy="scale_out", managed_by="python"),
                                None, {})
        assert "TP" not in msg.replace("Trade Opened", "")


class TestRiskBasis:
    def test_a_template_on_fixed_lots_says_nothing(self, settings):
        assert "Risk:" not in ta.fmt_trade_open(_trade(), None, {})

    def test_a_template_on_its_own_risk_pct_says_so(self, settings):
        settings["template"]["risk_pct"] = 1.5
        assert "Risk: 1.5% of balance" in ta.fmt_trade_open(_trade(), None, {})

    def test_the_global_override_is_named(self, settings):
        settings["rs"]["global_sizing_override"] = 1
        settings["rs"]["risk_per_trade_pct"] = 2.0
        assert "Risk: 2% of balance" in ta.fmt_trade_open(_trade(), None, {})

    def test_a_settings_read_that_fails_costs_the_line_not_the_alert(self, settings, monkeypatch):
        def _boom():
            raise RuntimeError("db gone")
        monkeypatch.setattr(ta.db_module, "get_risk_settings", _boom)
        msg = ta.fmt_trade_open(_trade(), None, {})
        assert "XAUUSD — Trade Opened" in msg
        assert "Risk:" not in msg
