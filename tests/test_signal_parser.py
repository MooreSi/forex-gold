"""Unit tests for signal parsers."""

import unittest

from backend.src.services.signals.parser import parse_gold_signal, parse_gd2_signal, validate_signal


class TestParseGoldSignal(unittest.TestCase):
    def _format_a(self):
        return (
            "This is not financial advice. Use appropriate risk management if you're going to trade.\n"
            "Sell Gold 4520 - 4512\n"
            "Stop Loss 4524\n"
            "TP1 4510  TP2 4508  TP3 4506  TP4 4503  TP5 4500"
        )

    def _format_b(self):
        return (
            "This is not financial advice. Use appropriate risk management if you're going to trade.\n"
            "Direction  SELL\n"
            "ENTRY : 4468-4466\n"
            "TP1: 4464  TP2: 4462  TP3: 4460\n"
            "SL: 4476"
        )

    def test_format_a_parses(self):
        r = parse_gold_signal(self._format_a())
        self.assertIsNotNone(r)
        self.assertEqual(r["direction"], "SELL")
        self.assertAlmostEqual(r["entry_low"],  4512.0, places=1)
        self.assertAlmostEqual(r["entry_high"], 4520.0, places=1)
        self.assertAlmostEqual(r["stop_loss"],  4524.0, places=1)
        self.assertAlmostEqual(r["tp1"],        4510.0, places=1)

    def test_format_b_parses(self):
        r = parse_gold_signal(self._format_b())
        self.assertIsNotNone(r)
        self.assertEqual(r["direction"], "SELL")
        self.assertAlmostEqual(r["stop_loss"], 4476.0, places=1)
        self.assertAlmostEqual(r["tp1"], 4464.0, places=1)

    def test_non_xauusd_rejected(self):
        text = (
            "This is not financial advice. Use appropriate risk management.\n"
            "Currency: EURUSD\n"
            "Buy Gold 1.1200 - 1.1180\n"
            "Stop Loss 1.1150\n"
            "TP1 1.1250"
        )
        self.assertIsNone(parse_gold_signal(text))

    def test_missing_sl_returns_none(self):
        text = "Buy Gold 4510 - 4508\nTP1 4520"
        self.assertIsNone(parse_gold_signal(text))

    def test_no_direction_returns_none(self):
        self.assertIsNone(parse_gold_signal("Just some random text with no signal"))


class TestParseGD2Signal(unittest.TestCase):
    def _sample(self):
        return (
            "XAU USD BUY NOW\n\n"
            "4534 - 4529\n\n"
            "TP1 4537\n"
            "TP2 4539\n"
            "TP3 4541\n"
            "TP4 4543\n"
            "TP5 4545\n\n"
            "SL 4527"
        )

    def test_gd2_parses(self):
        r = parse_gd2_signal(self._sample())
        self.assertIsNotNone(r)
        self.assertEqual(r["direction"], "BUY")
        self.assertAlmostEqual(r["entry_low"],  4529.0, places=1)
        self.assertAlmostEqual(r["entry_high"], 4534.0, places=1)
        self.assertAlmostEqual(r["stop_loss"],  4527.0, places=1)
        self.assertAlmostEqual(r["tp1"],        4537.0, places=1)
        self.assertAlmostEqual(r["tp5"],        4545.0, places=1)

    def test_gd2_sell(self):
        text = (
            "XAU USD SELL NOW\n"
            "4520 - 4525\n"
            "TP1 4510\n"
            "SL 4530"
        )
        r = parse_gd2_signal(text)
        self.assertIsNotNone(r)
        self.assertEqual(r["direction"], "SELL")

    def test_gd2_missing_tp1_returns_none(self):
        text = "XAU USD BUY NOW\n4534 - 4529\nSL 4527"
        self.assertIsNone(parse_gd2_signal(text))

    def test_gd2_commentary_rejected(self):
        text = "XAU USD BUY NOW - TP1 4537 hit!"
        r = parse_gd2_signal(text)
        # No entry range after direction — should return None
        self.assertIsNone(r)

    def test_gd2_zone_format_with_slash_invalid_sl(self):
        # Original Gold Diggers 2.0 "Zone" wording (2026-07-06).
        text = (
            "Buy Zone Now\n"
            "4163.5 - 4158.5\n"
            "Targets\n"
            "4165.5\n"
            "4167.5\n"
            "4170\n"
            "SL/ invalid 4155.5"
        )
        r = parse_gd2_signal(text)
        self.assertIsNotNone(r)
        self.assertEqual(r["direction"], "BUY")
        self.assertAlmostEqual(r["entry_low"],  4158.5, places=1)
        self.assertAlmostEqual(r["entry_high"], 4163.5, places=1)
        self.assertAlmostEqual(r["stop_loss"],  4155.5, places=1)
        self.assertAlmostEqual(r["tp1"],        4165.5, places=1)
        self.assertAlmostEqual(r["tp3"],        4170.0, places=1)

    def test_gd2_zone_format_plain_sl_next_buy_zone(self):
        # Gold Diggers Scalping's "Next Buy Zone" layout (2026-07-24) — SL is
        # a bare "SL <price>" line, no "/invalid" — previously fell through
        # to parse_gd2_partial forever since _GD2_SL_ZONE_RE only matched
        # the "/invalid" wording. Also confirms a "Zone" trigger preceded by
        # another word ("Next Buy Zone", not just "Buy Zone") still matches,
        # and a bare "Open" line among the numeric TPs is skipped as noise.
        text = (
            "Next Buy Zone\n\n"
            "4051 - 4047\n\n"
            "Targets \n\n"
            "4053.5\n"
            "4055.5\n"
            "Open\n\n"
            "SL 4043"
        )
        r = parse_gd2_signal(text)
        self.assertIsNotNone(r)
        self.assertEqual(r["direction"], "BUY")
        self.assertAlmostEqual(r["entry_low"],  4047.0, places=1)
        self.assertAlmostEqual(r["entry_high"], 4051.0, places=1)
        self.assertAlmostEqual(r["stop_loss"],  4043.0, places=1)
        self.assertAlmostEqual(r["tp1"],        4053.5, places=1)
        self.assertAlmostEqual(r["tp2"],        4055.5, places=1)

    def test_gd2_zone_format_plain_sl_with_colon(self):
        text = (
            "Sell Zone Now\n"
            "4200 - 4205\n"
            "Targets\n"
            "4190\n"
            "SL: 4210"
        )
        r = parse_gd2_signal(text)
        self.assertIsNotNone(r)
        self.assertEqual(r["direction"], "SELL")
        self.assertAlmostEqual(r["stop_loss"], 4210.0, places=1)


class TestValidateSignal(unittest.TestCase):
    def test_valid_buy(self):
        errors = validate_signal("BUY", 2340.0, 2342.0, 2330.0, 2360.0, 2370.0, None, None, None)
        self.assertEqual(errors, [])

    def test_valid_sell(self):
        errors = validate_signal("SELL", 2360.0, 2362.0, 2370.0, 2350.0, 2340.0, None, None, None)
        self.assertEqual(errors, [])

    def test_buy_sl_above_entry(self):
        errors = validate_signal("BUY", 2340.0, 2342.0, 2345.0, 2360.0, None, None, None, None)
        self.assertTrue(any("Stop Loss" in e for e in errors))

    def test_sell_sl_below_entry(self):
        errors = validate_signal("SELL", 2360.0, 2362.0, 2358.0, 2350.0, None, None, None, None)
        self.assertTrue(any("Stop Loss" in e for e in errors))

    def test_buy_tp_below_entry(self):
        errors = validate_signal("BUY", 2340.0, 2342.0, 2330.0, 2335.0, None, None, None, None)
        self.assertTrue(any("TP1" in e for e in errors))

    def test_invalid_direction(self):
        errors = validate_signal("HOLD", 2340.0, 2342.0, 2330.0, 2360.0, None, None, None, None)
        self.assertTrue(any("BUY or SELL" in e for e in errors))

    def test_entry_low_gt_high(self):
        errors = validate_signal("BUY", 2342.0, 2340.0, 2330.0, 2360.0, None, None, None, None)
        self.assertTrue(any("entry_low" in e for e in errors))

    def test_tp_ordering_buy(self):
        errors = validate_signal("BUY", 2340.0, 2342.0, 2330.0, 2360.0, 2355.0, None, None, None)
        self.assertTrue(any("TP" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
