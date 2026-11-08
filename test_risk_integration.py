
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import weekly_guard
from risk_manager import calculate_position_size, check_trade


class TestRiskIntegration(unittest.TestCase):

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

    def test_saved_weekly_limit_blocks_position_calculation(self):
        weekly_guard.record_loss(Decimal("2.00"))

        position, loss, message = calculate_position_size(
            balance=Decimal("20.00"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            weekly_loss=Decimal("0.00"),
        )

        self.assertEqual(position, Decimal("0"))
        self.assertEqual(loss, Decimal("0"))
        self.assertIn("Parada semanal", message)

    def test_saved_weekly_limit_blocks_trade_validation(self):
        weekly_guard.record_loss(Decimal("2.00"))

        approved, message = check_trade(
            balance=Decimal("20.00"),
            position_value=Decimal("5.00"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            weekly_loss=Decimal("0.00"),
        )

        self.assertFalse(approved)
        self.assertIn("Parada semanal", message)

    def test_saved_partial_loss_reduces_remaining_risk(self):
        weekly_guard.record_loss(Decimal("1.80"))

        position, estimated_loss, message = calculate_position_size(
            balance=Decimal("20.00"),
            entry_price=Decimal("100"),
            stop_price=Decimal("95"),
            weekly_loss=Decimal("0.00"),
        )

        self.assertGreater(
    position,
    Decimal("0"),
    f"Posición={position}; pérdida={estimated_loss}; mensaje={message}",
)
        self.assertLessEqual(estimated_loss, Decimal("0.20"))
        self.assertLess(
            position,
            Decimal("7.4074"),
        )


if __name__ == "__main__":
    unittest.main()
