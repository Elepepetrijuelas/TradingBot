"""Aislamiento de persistencia y recuperación del paper trader."""
import csv
import tempfile
from types import SimpleNamespace
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import paper_trader


def sample_trade():
    return {
        "time": "2026-11-02T05:00:00+00:00",
        "reason": "Stop loss",
        "entry_time": "2026-11-02T01:00:00+00:00",
        "entry": "100",
        "exit": "99",
        "size": "0.1",
        "gross": "-0.1",
        "costs": "0.02",
        "net": "-0.12",
        "capital": "19.88",
        "weekly_loss_after_trade": "0.12",
    }


class PaperTraderPersistenceTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).with_name(".test_tmp")
        test_root.mkdir(exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=test_root)
        self.directory = Path(self.temp_dir.name)
        self.state_path = self.directory / "paper_state.json"
        self.trades_path = self.directory / "paper_trades.csv"
        self.state_patch = patch.object(paper_trader, "STATE_FILE", self.state_path)
        self.trades_patch = patch.object(paper_trader, "TRADES_FILE", self.trades_path)
        self.state_patch.start()
        self.trades_patch.start()

    def tearDown(self):
        self.trades_patch.stop()
        self.state_patch.stop()
        self.temp_dir.cleanup()

    def test_state_is_saved_and_loaded(self):
        state = paper_trader.new_state()
        state["capital"] = Decimal("19.88")
        paper_trader.save_state(state)

        loaded = paper_trader.load_state()

        self.assertEqual(loaded["capital"], Decimal("19.88"))
        self.assertEqual(loaded["trades"], [])

    def test_failed_state_replace_preserves_previous_file(self):
        state = paper_trader.new_state()
        paper_trader.save_state(state)
        previous = self.state_path.read_bytes()
        state["capital"] = Decimal("19.88")

        with patch.object(paper_trader.os, "replace", side_effect=OSError("fallo simulado")):
            with self.assertRaises(OSError):
                paper_trader.save_state(state)

        self.assertEqual(self.state_path.read_bytes(), previous)
        self.assertEqual(paper_trader.load_state()["capital"], paper_trader.CFG.initial_capital)

    def test_corrupt_or_incoherent_state_is_rejected(self):
        self.state_path.write_text("{json roto", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            paper_trader.load_state()

        state = paper_trader.new_state()
        state["in_position"] = True
        paper_trader.save_state(state)
        with self.assertRaises(RuntimeError):
            paper_trader.load_state()

    def test_open_position_is_recovered(self):
        state = paper_trader.new_state()
        state.update({
            "in_position": True,
            "position_size": Decimal("0.1"),
            "entry_price": Decimal("100"),
            "stop_price": Decimal("95"),
            "entry_time": "2026-11-02T01:00:00+00:00",
        })
        paper_trader.save_state(state)

        loaded = paper_trader.load_state()

        self.assertTrue(loaded["in_position"])
        self.assertEqual(loaded["position_size"], Decimal("0.1"))
        self.assertEqual(loaded["entry_price"], Decimal("100"))
        self.assertEqual(loaded["stop_price"], Decimal("95"))
        self.assertEqual(loaded["entry_time"], "2026-11-02T01:00:00+00:00")

    def test_ledger_is_reconstructed_from_state_trades(self):
        trades = [sample_trade()]
        state = paper_trader.new_state()
        state["trades"] = trades
        paper_trader.save_state(state)
        self.trades_path.write_text("CSV desactualizado\n", encoding="utf-8")

        paper_trader.save_trade_ledger(paper_trader.load_state()["trades"])

        with self.trades_path.open("r", encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        self.assertEqual(rows, trades)

    def test_failed_ledger_replace_preserves_previous_file(self):
        previous_trade = sample_trade()
        paper_trader.save_trade_ledger([previous_trade])
        previous = self.trades_path.read_bytes()

        with patch.object(paper_trader.os, "replace", side_effect=OSError("fallo simulado")):
            with self.assertRaises(OSError):
                paper_trader.save_trade_ledger([])

        self.assertEqual(self.trades_path.read_bytes(), previous)

    def test_run_once_rebuilds_ledger_before_fetching_candles(self):
        events = []
        context_end = datetime(2026, 10, 31, 3, tzinfo=timezone.utc)
        context = [{"time": context_end}]

        def load_state():
            events.append("load_state")
            return paper_trader.new_state()

        def save_ledger(_trades):
            events.append("save_ledger")

        def read_candles():
            events.append("read_candles")
            return {}

        def download(*_args):
            events.append("download")
            return 0

        with (
            patch.object(paper_trader.data_feed, "verify_frozen_configuration"),
            patch.object(
                paper_trader,
                "CFG",
                SimpleNamespace(live_trading_enabled=False, initial_capital=Decimal("20")),
            ),
            patch.object(paper_trader.engine, "load_candles", return_value=context),
            patch.object(paper_trader, "load_state", side_effect=load_state),
            patch.object(paper_trader, "save_trade_ledger", side_effect=save_ledger),
            patch.object(paper_trader.data_feed, "read_forward_candles", side_effect=read_candles),
            patch.object(paper_trader.data_feed, "download_closed_candles", side_effect=download),
            patch.object(paper_trader.data_feed, "verify_contiguous", return_value=[]),
            patch.object(paper_trader, "process_new_candles", return_value=(0, [])),
        ):
            paper_trader.run_once()

        self.assertEqual(events[:4], ["load_state", "save_ledger", "read_candles", "download"])
        self.assertEqual(events.count("save_ledger"), 2)  # inicio y guardado final


if __name__ == "__main__":
    unittest.main()
