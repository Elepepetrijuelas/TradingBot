
import unittest
from decimal import Decimal
from unittest.mock import patch

import weekly_guard


class TestWeeklyGuard(unittest.TestCase):

    def setUp(self):
        self.temp_state = self.enterContext(
            patch.object(
                weekly_guard,
                "STATE_FILE",
                weekly_guard.Path(__file__).with_name(
                    "weekly_state_test.json"
                ),
            )
        )
        self.temp_state.unlink(missing_ok=True)

    def tearDown(self):
        self.temp_state.unlink(missing_ok=True)

    def test_initial_state_allows_trading(self):
        state = weekly_guard.load_state()

        self.assertEqual(
            Decimal(state["weekly_loss"]),
            Decimal("0.00"),
        )
        self.assertFalse(state["halted"])
        self.assertTrue(weekly_guard.trading_allowed())

    def test_loss_is_recorded(self):
        state = weekly_guard.record_loss("0.50")

        self.assertEqual(
            Decimal(state["weekly_loss"]),
            Decimal("0.50"),
        )
        self.assertTrue(weekly_guard.trading_allowed())

    def test_reaching_weekly_limit_blocks_trading(self):
        weekly_guard.record_loss("1.00")
        state = weekly_guard.record_loss("1.00")

        self.assertTrue(state["halted"])
        self.assertFalse(weekly_guard.trading_allowed())

    def test_block_persists_after_reload(self):
        weekly_guard.record_loss("2.00")

        # Simula una nueva lectura del estado desde disco.
        state = weekly_guard.load_state()

        self.assertTrue(state["halted"])
        self.assertFalse(weekly_guard.trading_allowed())

    def test_invalid_loss_is_rejected(self):
        for amount in ("0", "-1", "NaN", "Infinity", "abc"):
            with self.subTest(amount=amount):
                with self.assertRaises((ValueError, ArithmeticError)):
                    weekly_guard.record_loss(amount)


    def test_corrupted_state_fails_closed(self):
        self.temp_state.write_text(
            '{"week_id": ',
            encoding="utf-8",
        )

        with self.assertRaises(RuntimeError):
            weekly_guard.trading_allowed()

    def test_new_week_resets_weekly_loss(self):
        weekly_guard.STATE_FILE.write_text(
            '{"week_id": "2000-W01", '
            '"weekly_loss": "1.50", '
            '"halted": false}',
            encoding="utf-8",
        )

        state = weekly_guard.load_state()

        self.assertEqual(
            state["week_id"],
            weekly_guard.current_week_id(),
        )
        self.assertEqual(
            Decimal(state["weekly_loss"]),
            Decimal("0.00"),
        )
        self.assertFalse(state["halted"])

if __name__ == "__main__":
    unittest.main(verbosity=2)
