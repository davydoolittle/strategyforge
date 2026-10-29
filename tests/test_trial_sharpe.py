"""
Tests for per-trial Sharpe stored on candidates (P2.3). In-memory SQLite.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import optimizer  # noqa: E402
import validation as V  # noqa: E402

RISK_SIZED = {"use_risk_sizing": True, "risk_pct": 1.0, "initial_capital": 10000.0}


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

    def add_series(self, rets, cfg=RISK_SIZED):
        """Store a trial built from per-trade returns (fractions of equity)."""
        trades, eq = [], cfg["initial_capital"]
        for r in rets:
            trades.append({"pnl": r * eq})
            eq += r * eq
        sr, _, _ = optimizer.trade_stats(trades, cfg["initial_capital"])
        cur = self.conn.execute(
            "INSERT INTO candidates (pair, tf, params_json, is_trades, is_net, is_sharpe)"
            " VALUES ('BTC-USD', '1h', ?, ?, ?, ?)",
            (json.dumps(cfg), len(trades), eq - cfg["initial_capital"], sr))
        return cur.lastrowid, sr, trades

    def test_identical_stop_outs_are_degenerate(self):
        # F11's #5518: ten 1% stop-outs that differ only by cost noise
        rets = [-0.0110 - 0.0001 * (i % 3) for i in range(10)]
        cid, sr, trades = self.add_series(rets)
        self.assertLess(sr, -10)
        true_sd = V._std(V.trade_returns(trades, 10000))
        est = V.per_trade_return_std(sr, 10, sum(t["pnl"] for t in trades), 10000)
        self.assertAlmostEqual(est, true_sd, delta=0.05 * true_sd)
        self.assertTrue(V.is_degenerate_trial(sr, 10, sum(t["pnl"] for t in trades),
                                              json.dumps(RISK_SIZED)))
        self.assertEqual(V.degenerate_trials(self.conn, "BTC-USD", "1h"), [cid])

    def test_ordinary_stop_and_target_series_is_kept(self):
        # 2R targets and 1R stops, all losses: still not degenerate if they vary
        for rets in ([0.02, -0.01, -0.01, 0.02, -0.01, -0.01, 0.02, -0.01, -0.01, -0.01],
                     [-0.011, -0.004, -0.010, -0.007, -0.011, -0.002, -0.010, -0.009, -0.011, -0.006]):
            _, sr, trades = self.add_series(rets)
            self.assertFalse(V.is_degenerate_trial(sr, 10, sum(t["pnl"] for t in trades),
                                                   json.dumps(RISK_SIZED)))
        self.assertEqual(V.degenerate_trials(self.conn, "BTC-USD", "1h"), [])

    def test_degenerate_trial_leaves_variance_but_counts_in_n(self):
        kept = [self.add_series(r)[1] for r in (
            [0.02, -0.01, -0.01, 0.02, -0.01, -0.01, 0.02, -0.01, -0.01, -0.01],
            [0.02, 0.02, -0.01, 0.02, -0.01, -0.01, 0.02, -0.01, 0.02, -0.01],
            [-0.01, -0.01, -0.01, 0.02, -0.01, -0.01, -0.01, -0.01, 0.02, -0.01])]
        self.add_series([-0.0110 - 0.0001 * (i % 3) for i in range(10)])
        var, used, total, missing = V.sharpe_variance_from_candidates(self.conn, "BTC-USD", "1h")
        self.assertEqual((used, total, missing), (3, 4, 0))
        self.assertAlmostEqual(var, V._std(kept) ** 2)

    def test_without_risk_sizing_never_degenerate(self):
        cfg = dict(RISK_SIZED, use_risk_sizing=False)
        self.add_series([-0.0110 - 0.0001 * (i % 3) for i in range(10)], cfg)
        self.assertEqual(V.degenerate_trials(self.conn, "BTC-USD", "1h"), [])
        self.assertFalse(V.is_degenerate_trial(-19.0, 10, None, json.dumps(RISK_SIZED)))

    def test_empty(self):
        self.assertEqual(V.sharpe_variance_from_candidates(self.conn, "BTC-USD", "1h"),
                         (0.0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
