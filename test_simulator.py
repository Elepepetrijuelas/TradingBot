
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import weekly_guard
from simulator import simulate_trade


class TestSimulator(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "weekly_state.json"
        self.patcher = patch.object(
            weekly_guard,
            "STATE_FILE",
            self.state_file,
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    def test_profitable_trade_increases_balance(self):
        result = simulate_trade(
            balance=Decimal("20.00"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            exit_price=Decimal("103"),
        )

        self.assertTrue(result["approved"])
        self.assertGreater(result["net_pnl"], Decimal("0"))
        self.assertGreater(result["new_balance"], Decimal("20.00"))
        self.assertEqual(
            result["weekly_loss_after_trade"],
            Decimal("0"),
        )

    def test_losing_trade_reduces_balance(self):
        result = simulate_trade(
            balance=Decimal("20.00"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            exit_price=Decimal("95"),
        )

        self.assertTrue(result["approved"])
        self.assertLess(result["net_pnl"], Decimal("0"))
        self.assertLess(result["new_balance"], Decimal("20.00"))
        self.assertGreater(
            result["weekly_loss_after_trade"],
            Decimal("0"),
        )

    def test_weekly_limit_blocks_new_trade(self):
        result = simulate_trade(
            balance=Decimal("20.00"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            exit_price=Decimal("103"),
            weekly_loss=Decimal("2.00"),
        )

        self.assertFalse(result["approved"])

    def test_nonpositive_exit_price_is_rejected(self):
        with self.assertRaises(ValueError):
            simulate_trade(
                balance=Decimal("20.00"),
                entry_price=Decimal("100"),
                stop_price=Decimal("95"),
                exit_price=Decimal("0"),
            )

    def test_simulation_does_not_modify_persistent_weekly_state(self):
        result = simulate_trade(
            balance=Decimal("20.00"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            exit_price=Decimal("95"),
        )

        self.assertTrue(result["approved"])
        self.assertFalse(
            self.state_file.exists(),
            "El simulador de pruebas no debe alterar el registro persistente.",
        )


if __name__ == "__main__":
    unittest.main()
