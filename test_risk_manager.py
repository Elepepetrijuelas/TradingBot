
import unittest
from decimal import Decimal

from config import (
    PROTECTED_RESERVE,
    RISK_BUDGET,
    WEEKLY_LOSS_LIMIT,
)
from risk_manager import (
    calculate_position_size,
    check_trade,
    loss_fraction,
)


class TestRiskManager(unittest.TestCase):

    def setUp(self):
        self.balance = Decimal("20.00")
        self.entry = Decimal("100")
        self.stop = Decimal("95")
        self.no_weekly_loss = Decimal("0.00")

    # Las cinco pruebas originales

    def test_valid_trade_is_accepted(self):
        position, loss, message = calculate_position_size(
            self.balance,
            self.entry,
            self.stop,
            self.no_weekly_loss,
        )

        approved, result = check_trade(
            self.balance,
            position,
            self.entry,
            self.stop,
            self.no_weekly_loss,
        )

        self.assertGreater(position, Decimal("0"))
        self.assertLessEqual(loss, RISK_BUDGET)
        self.assertTrue(approved, result)

    def test_excessive_risk_is_rejected(self):
        approved, message = check_trade(
            self.balance,
            Decimal("8.00"),
            self.entry,
            self.stop,
            self.no_weekly_loss,
        )

        self.assertFalse(approved)
        self.assertIn("Riesgo estimado", message)

    def test_weekly_loss_limit_blocks_trade(self):
        approved, message = check_trade(
            self.balance,
            Decimal("1.00"),
            self.entry,
            self.stop,
            WEEKLY_LOSS_LIMIT,
        )

        self.assertFalse(approved)
        self.assertIn("Parada semanal", message)

    def test_protected_reserve_cannot_be_used(self):
        approved, message = check_trade(
            PROTECTED_RESERVE,
            Decimal("1.00"),
            self.entry,
            self.stop,
            self.no_weekly_loss,
        )

        self.assertFalse(approved)
        self.assertIn("presupuesto operativo", message)

    def test_invalid_stop_is_rejected(self):
        approved, message = check_trade(
            self.balance,
            Decimal("1.00"),
            self.entry,
            self.entry,
            self.no_weekly_loss,
        )

        self.assertFalse(approved)
        self.assertIn("stop", message.lower())

    # Pruebas adicionales de límites y casos extremos

    def test_exact_risk_limit_is_allowed(self):
        fraction = loss_fraction(self.entry, self.stop)
        position = RISK_BUDGET / fraction

        approved, message = check_trade(
            self.balance,
            position,
            self.entry,
            self.stop,
        )

        self.assertTrue(approved, message)

    def test_weekly_loss_just_below_limit_is_allowed(self):
        approved, message = check_trade(
            self.balance,
            Decimal("0.10"),
            Decimal("100"),
            Decimal("99"),
            Decimal("1.99"),
        )

        self.assertTrue(approved, message)

    def test_weekly_loss_exactly_at_limit_is_rejected(self):
        approved, message = check_trade(
            self.balance,
            Decimal("1.00"),
            self.entry,
            self.stop,
            WEEKLY_LOSS_LIMIT,
        )

        self.assertFalse(approved)
        self.assertIn("Parada semanal", message)

    def test_weekly_loss_reaching_limit_is_rejected(self):
        entry = Decimal("100")
        stop = Decimal("95")
        fraction = loss_fraction(entry, stop)

        # Construye una pérdida estimada de $0.10.
        position = Decimal("0.10") / fraction

        approved, message = check_trade(
            self.balance,
            position,
            entry,
            stop,
            Decimal("1.90"),
        )

        self.assertFalse(approved, message)

    def test_balance_at_reserve_cannot_fund_trade(self):
        approved, message = check_trade(
            PROTECTED_RESERVE,
            Decimal("1.00"),
            self.entry,
            self.stop,
        )

        self.assertFalse(approved)
        self.assertIn("presupuesto operativo", message)

    def test_negative_values_are_rejected(self):
        approved, _ = check_trade(
            Decimal("-5"),
            Decimal("5"),
            self.entry,
            self.stop,
        )
        self.assertFalse(approved)

        approved, _ = check_trade(
            self.balance,
            Decimal("5"),
            Decimal("-100"),
            Decimal("98"),
        )
        self.assertFalse(approved)

    def test_calculated_position_respects_risk_limit(self):
        position, loss, message = calculate_position_size(
            self.balance,
            self.entry,
            self.stop,
            self.no_weekly_loss,
        )

        self.assertGreater(position, Decimal("0"), message)
        self.assertLessEqual(loss, RISK_BUDGET)

    def test_fees_and_slippage_are_included(self):
        fraction = loss_fraction(
            Decimal("100"),
            Decimal("95"),
        )

        self.assertEqual(fraction, Decimal("0.054"))

    def test_negative_weekly_loss_is_rejected(self):
        position, loss, message = calculate_position_size(
            Decimal("20"),
            Decimal("100"),
            Decimal("95"),
            Decimal("-0.01"),
        )

        self.assertEqual(position, Decimal("0"))
        self.assertEqual(loss, Decimal("0"))
        self.assertIn("negativa", message.lower())

if __name__ == "__main__":
    unittest.main(verbosity=2)
