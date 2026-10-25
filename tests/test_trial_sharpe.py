"""
Tests for per-trial Sharpe stored on candidates (P2.3). In-memory SQLite.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import optimizer  # noqa: E402
import validation as V  # noqa: E402


class TestTradeStats(unittest.TestCase):
    def test_matches_validation(self):
        trades = [{"pnl": p} for p in (120, -80, 90, -60, 200, -150, 40)]
        sr, skew, kurt = optimizer.trade_stats(trades, 10000)
        rets = V.trade_returns(trades, 10000)
        self.assertAlmostEqual(sr, V.sharpe(rets))
        self.assertEqual((skew, kurt), V.moments(rets))

    def test_no_trades(self):
        self.assertEqual(optimizer.trade_stats([], 10000), (0.0, 0.0, 3.0))


class TestVarianceFromCandidates(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        optimizer.ensure_tables(self.conn)

    def add(self, sr, n, pair="BTC-USD", tf="1h"):
        self.conn.execute("INSERT INTO candidates (pair, tf, params_json, is_trades, is_sharpe)"
                          " VALUES (?, ?, '{}', ?, ?)", (pair, tf, n, sr))

    def test_uses_all_qualifying_trials(self):
        for sr in (0.1, 0.3, -0.2, 0.0):
            self.add(sr, 40)
        self.add(7.0, 2)                 # degenerate: excluded from variance
        self.add(None, 30)               # not backfilled: excluded, but reported
        self.add(5.0, 40, pair="ETH-USD")
        self.add(5.0, 40, tf="4h")
        var, used, total, missing = V.sharpe_variance_from_candidates(self.conn, "BTC-USD", "1h")
        self.assertEqual((used, total, missing), (4, 6, 1))
        self.assertAlmostEqual(var, V._std([0.1, 0.3, -0.2, 0.0]) ** 2)

    def test_empty(self):
        self.assertEqual(V.sharpe_variance_from_candidates(self.conn, "BTC-USD", "1h"),
                         (0.0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
