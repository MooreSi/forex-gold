"""Safety and causality checks for the isolated research prototype."""
import unittest
import pandas as pd
from prototype import causal_train, net_virtual_r, select_trades


class PrototypeChecks(unittest.TestCase):
    def test_future_and_unresolved_labels_are_excluded(self):
        rows = pd.DataFrame({"created_at": [10, 20, 200],
                             "label_available_at": [30, 150, 210]})
        self.assertEqual(causal_train(rows, 100, 10).index.tolist(), [0])

    def test_broker_dollars_are_never_scaled_as_virtual_r(self):
        self.assertIsNone(net_virtual_r({"live_exec_status": "executed",
                                       "net_pnl_dollars": 100, "sl_dist": 5}))

    def test_net_cost_is_not_charged_twice(self):
        self.assertEqual(net_virtual_r({"net_pnl_dollars": 20, "sl_dist": 5}), 0.4)

    def test_bad_inputs_are_rejected(self):
        for value in [float("nan"), float("inf"), None]:
            self.assertIsNone(net_virtual_r({"net_pnl_dollars": value, "sl_dist": 5}))

    def test_uncertainty_and_invalid_scores_abstain(self):
        self.assertEqual(select_trades([-0.1, 0, 0.06, float("nan")], 0.05).tolist(),
                         [False, False, True, False])


if __name__ == "__main__":
    unittest.main()
