
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import backtest_engine as engine


class TestBacktestEngineIntegrity(unittest.TestCase):

    def make_candle(self, timestamp, base):
        return {
            "time": timestamp,
            "open": Decimal(str(base)),
            "high": Decimal(str(base + 3)),
            "low": Decimal(str(base - 1)),
            "close": Decimal(str(base + 2)),
            "volume": Decimal("1"),
        }

    def test_segment_starts_detects_hourly_gap(self):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        candles = [
            self.make_candle(start, 100),
            self.make_candle(start + timedelta(hours=1), 102),
            self.make_candle(start + timedelta(hours=2), 104),
            self.make_candle(start + timedelta(hours=4), 106),
            self.make_candle(start + timedelta(hours=5), 108),
        ]

        segments = engine._build_segment_starts(candles)

        self.assertEqual(segments, [0, 0, 0, 3, 3])

    def test_segmented_moving_average_resets_after_gap(self):
        values = [
            Decimal("10"),
            Decimal("20"),
            Decimal("30"),
            Decimal("100"),
            Decimal("200"),
        ]
        segments = [0, 0, 0, 3, 3]

        result = [
            engine._segmented_moving_average(
                values,
                period=2,
                index=i,
                segment_starts=segments,
            )
            for i in range(len(values))
        ]

        self.assertIsNone(result[0])
        self.assertEqual(result[1], Decimal("15"))
        self.assertEqual(result[2], Decimal("25"))
        self.assertIsNone(result[3])
        self.assertEqual(result[4], Decimal("150"))

    def test_adx_resets_after_hourly_gap(self):
        period = 3
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)

        timestamps = (
            [start + timedelta(hours=i) for i in range(7)]
            + [start + timedelta(hours=10 + i) for i in range(7)]
        )

        candles = [
            self.make_candle(timestamp, 100 + i * 2)
            for i, timestamp in enumerate(timestamps)
        ]

        adx_values = engine.calculate_adx(candles, period=period)

        self.assertEqual(len(adx_values), len(candles))

        for index in range(7, 12):
            self.assertIsNone(adx_values[index])

        self.assertIsNotNone(adx_values[12])
        self.assertIsNotNone(adx_values[13])

    def test_loss_is_recorded_in_week_of_trade_close(self):
        # La entrada debe ocurrir el domingo a las 23:00 UTC.
        # La operación se cierra el lunes a las 00:00 UTC.
        start = datetime(2026, 5, 2, 14, 0, tzinfo=timezone.utc)

        prices = (
            [Decimal("100")] * 200
            + [Decimal("110")] * 2
            + [Decimal("90")] * 5
        )

        candles = []

        for i, price in enumerate(prices):
            candles.append({
                "time": start + timedelta(hours=i),
                "open": price,
                "high": price + Decimal("0.05"),
                "low": price - Decimal("0.05"),
                "close": price,
            })

        final_capital, trades, max_drawdown = engine.run_backtest(candles)

        self.assertTrue(trades, "Debería haberse ejecutado una operación")

        trade = trades[0]

        self.assertEqual(
            trade["time"],
            datetime(2026, 5, 11, 0, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(trade["reason"], "Stop con gap")

        self.assertGreater(
            trade["weekly_loss_after_trade"],
            Decimal("0"),
            "La pérdida debe contabilizarse en la semana del cierre",
        )

        self.assertLess(final_capital, Decimal("20.00"))
        self.assertGreater(max_drawdown, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
