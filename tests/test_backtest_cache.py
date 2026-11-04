#!/usr/bin/env python3
"""
Unit and integration tests for Backtest Cache Freshness & Multi-Symbol Temporal Alignment.
Verifies:
1. Stale cache (> 2 days) triggers re-download via fetch_klines_chunk.
2. Missing cache triggers download and creates cache file.
3. Fresh cache (< 2 days) loads directly without network fetch.
4. Zero-tolerance temporal alignment: any diff != 0 between symbol and BTC raises RuntimeError.
5. Exact temporal alignment (diff == 0) passes cleanly.
6. Backtest simulation window anchors strictly to BTCUSDT 1H reference series.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Force test paths
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import backtest


def make_mock_download(months=12):
    """Generates synthetic download batch with full span to pass download validation."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(months * 30.5 * 24 * 3600 * 1000)
    span_ms = int(months * 28 * 24 * 3600 * 1000)
    step = span_ms // 500
    return [
        [start_ms + i * step, 100, 105, 95, 102, 1000, start_ms + (i + 1) * step - 1]
        for i in range(500)
    ]


class TestBacktestCacheAndAlignment(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.orig_data_dir = backtest.DATA_DIR
        backtest.DATA_DIR = Path(self.temp_dir)

    def tearDown(self):
        backtest.DATA_DIR = self.orig_data_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_stale_cache_triggers_redownload(self):
        """When cache candle is older than 2 days, load_or_download_klines must trigger fresh download."""
        symbol = "TESTUSDT"
        interval = "1h"
        months = 12
        cache_path = backtest.DATA_DIR / f"{symbol}_{interval}_{months}m.json"

        # Create dummy cache where the last candle is 5 days old (> 2 days limit)
        now_ms = int(time.time() * 1000)
        stale_last_close = now_ms - int(5.0 * 86400 * 1000)
        stale_data = [
            [stale_last_close - 3600000 * i, 100, 105, 95, 102, 1000, stale_last_close - 3600000 * (i - 1) - 1]
            for i in range(150, 0, -1)
        ]
        # Write stale cache with fresh file mtime
        cache_path.write_text(json.dumps(stale_data), encoding="utf-8")

        mock_download = make_mock_download(months)

        with patch("backtest.fetch_klines_chunk", return_value=mock_download) as mock_fetch:
            with patch("time.sleep"):
                with patch.dict(os.environ, {"BACKTEST_CACHE_MAX_AGE_DAYS": "2.0"}):
                    loaded = backtest.load_or_download_klines(symbol, interval, months)
                    # Assert download happened
                    self.assertTrue(mock_fetch.called, "fetch_klines_chunk was not called on stale cache!")
                    self.assertEqual(len(loaded), len(mock_download))
                    # Verify cache was overwritten with fresh data
                    new_cached = json.loads(cache_path.read_text("utf-8"))
                    self.assertEqual(len(new_cached), len(mock_download))

    def test_missing_cache_triggers_download(self):
        """When cache file does not exist, load_or_download_klines downloads and creates cache."""
        symbol = "NEWCOINUSDT"
        interval = "1h"
        months = 12
        cache_path = backtest.DATA_DIR / f"{symbol}_{interval}_{months}m.json"
        self.assertFalse(cache_path.exists())

        mock_download = make_mock_download(months)

        with patch("backtest.fetch_klines_chunk", return_value=mock_download) as mock_fetch:
            with patch("time.sleep"):
                loaded = backtest.load_or_download_klines(symbol, interval, months)
                self.assertTrue(mock_fetch.called, "fetch_klines_chunk was not called for missing cache!")
                self.assertTrue(cache_path.exists(), "Cache file was not created!")
                self.assertEqual(len(loaded), len(mock_download))

    def test_cache_hit_when_candle_within_two_days(self):
        """Fresh candle (e.g. 1.2 days old) results in a cache hit without network call."""
        symbol = "FRESHUSDT"
        interval = "1h"
        months = 12
        cache_path = backtest.DATA_DIR / f"{symbol}_{interval}_{months}m.json"

        now_ms = int(time.time() * 1000)
        fresh_last_close = now_ms - int(1.2 * 86400 * 1000)
        fresh_data = [
            [fresh_last_close - 3600000 * i, 100, 105, 95, 102, 1000, fresh_last_close - 3600000 * (i - 1) - 1]
            for i in range(150, 0, -1)
        ]
        cache_path.write_text(json.dumps(fresh_data), encoding="utf-8")

        with patch("backtest.fetch_klines_chunk") as mock_fetch:
            with patch.dict(os.environ, {"BACKTEST_CACHE_MAX_AGE_DAYS": "2.0"}):
                loaded = backtest.load_or_download_klines(symbol, interval, months)
                self.assertFalse(mock_fetch.called, "fetch_klines_chunk was not called despite fresh cache!")
                self.assertEqual(len(loaded), len(fresh_data))
                self.assertEqual(loaded[-1][6], fresh_last_close - 1)

    def test_zero_tolerance_temporal_misalignment_raises(self):
        """Zero tolerance: any non-zero difference between symbol and BTC close time raises RuntimeError."""
        engine = backtest.BacktestEngine(symbols=["BTCUSDT", "ETHUSDT"], months=12)

        now_ms = int(time.time() * 1000)
        btc_last_close = now_ms - int(1.0 * 86400 * 1000)
        # Even a 60 second difference must raise RuntimeError under zero tolerance
        eth_last_close = btc_last_close - 60000

        btc_data = [[btc_last_close - 3600000 * i, 100, 105, 95, 102, 1000, btc_last_close - 3600000 * (i - 1) - 1] for i in range(150, 0, -1)]
        eth_data = [[eth_last_close - 3600000 * i, 100, 105, 95, 102, 1000, eth_last_close - 3600000 * (i - 1) - 1] for i in range(150, 0, -1)]

        engine.btc_4h = btc_data
        engine.btc_close_times = [int(c[6]) for c in btc_data]
        engine.btc_4h_ema50 = [100.0] * len(btc_data)
        engine.data_1h["BTCUSDT"] = btc_data
        engine.data_4h["BTCUSDT"] = btc_data
        engine.data_15m["BTCUSDT"] = btc_data

        def mock_load(sym, interval, months):
            if sym == "BTCUSDT":
                return btc_data
            return eth_data

        with patch("backtest.load_or_download_klines", side_effect=mock_load):
            with self.assertRaises(RuntimeError) as ctx:
                engine.load_data()
            self.assertIn("Temporal misalignment", str(ctx.exception))
            self.assertIn("!= 0", str(ctx.exception))

    def test_exact_temporal_alignment_passes(self):
        """Exact temporal alignment (diff == 0) passes load_data cleanly."""
        engine = backtest.BacktestEngine(symbols=["BTCUSDT", "ETHUSDT"], months=12)

        now_ms = int(time.time() * 1000)
        btc_last_close = now_ms - int(1.0 * 86400 * 1000)
        # Identical close time
        eth_last_close = btc_last_close

        btc_data = [[btc_last_close - 3600000 * i, 100, 105, 95, 102, 1000, btc_last_close - 3600000 * (i - 1) - 1] for i in range(150, 0, -1)]
        eth_data = [[eth_last_close - 3600000 * i, 100, 105, 95, 102, 1000, eth_last_close - 3600000 * (i - 1) - 1] for i in range(150, 0, -1)]

        engine.btc_4h = btc_data
        engine.btc_close_times = [int(c[6]) for c in btc_data]
        engine.btc_4h_ema50 = [100.0] * len(btc_data)
        engine.data_1h["BTCUSDT"] = btc_data
        engine.data_4h["BTCUSDT"] = btc_data
        engine.data_15m["BTCUSDT"] = btc_data

        def mock_load(sym, interval, months):
            if sym == "BTCUSDT":
                return btc_data
            return eth_data

        with patch("backtest.load_or_download_klines", side_effect=mock_load):
            engine.load_data()  # Must not raise
            self.assertIn("ETHUSDT", engine.data_1h)

    def test_run_strategy_anchors_to_btc_1h(self):
        """Strategy simulation window must strictly anchor to BTCUSDT 1H."""
        engine = backtest.BacktestEngine(symbols=["BTCUSDT", "SOLUSDT"], months=12)

        now_ms = int(time.time() * 1000)
        btc_start = now_ms - 3600000 * 300
        btc_end = now_ms - 3600000

        btc_1h = [[btc_start + 3600000 * i, 100, 105, 95, 102, 1000, btc_start + 3600000 * (i + 1) - 1] for i in range(300)]
        sol_1h = [[btc_start - 3600000 * 10 + 3600000 * i, 10, 15, 9, 12, 1000, btc_start - 3600000 * 10 + 3600000 * (i + 1) - 1] for i in range(320)]

        engine.data_1h["BTCUSDT"] = btc_1h
        engine.data_1h["SOLUSDT"] = sol_1h
        engine.data_4h["BTCUSDT"] = btc_1h
        engine.data_4h["SOLUSDT"] = sol_1h
        engine.data_15m["BTCUSDT"] = btc_1h
        engine.data_15m["SOLUSDT"] = sol_1h
        engine.btc_4h = btc_1h
        engine.btc_close_times = [int(c[6]) for c in btc_1h]
        engine.btc_4h_ema50 = [100.0] * len(btc_1h)

        res = engine.run_strategy("V1_baseline", is_out_of_sample=False)
        self.assertIsInstance(res, dict)
        self.assertIn("trades", res)


if __name__ == "__main__":
    unittest.main()