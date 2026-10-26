"""
Tests for the effective number of trials (validation.effective_trials and
friends) and neff.py's store/lookup path. Synthetic series; the DB path uses
a fake engine.backtest so trades are known by construction.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import os
import random
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import engine  # noqa: E402
import neff as NE  # noqa: E402
import optimizer  # noqa: E402
import validation as V  # noqa: E402

DAY = 86400


def noisy_copies(base, k, rng, noise=0.05):
    return [[v + rng.gauss(0, noise) for v in base] for _ in range(k)]


class TestBucketReturns(unittest.TestCase):
    def test_trade_spread_pro_rata_over_days(self):
        t = {"entry_ts": DAY + DAY // 2, "exit_ts": 2 * DAY + DAY // 2, "pnl": 100.0}
        s = V.bucket_returns([t], 10_000.0, 0, 3 * DAY)
        self.assertEqual(len(s), 4)
        self.assertAlmostEqual(s[1], 0.005)
        self.assertAlmostEqual(s[2], 0.005)
        self.assertAlmostEqual(sum(s), 0.01)

    def test_outside_window_ignored(self):
        t = {"entry_ts": 10 * DAY, "exit_ts": 10 * DAY + 3600, "pnl": 100.0}
        self.assertEqual(sum(V.bucket_returns([t], 10_000.0, 0, 3 * DAY)), 0.0)

    def test_rebucket(self):
        self.assertEqual(V.rebucket([1, 2, 3, 4, 5, 6, 7, 8], 7), [28, 8])


class TestCorrelation(unittest.TestCase):
    def test_identical_opposite_flat(self):
        a = [1.0, 2.0, 0.0, 5.0]
        c = V.correlation_matrix([a, a[:], [-x for x in a], [0.0] * 4])
        self.assertAlmostEqual(c[0][1], 1.0)
        self.assertAlmostEqual(c[0][2], -1.0)
        self.assertIsNone(c[3][3])
        self.assertIsNone(c[0][3])

    def test_distance(self):
        self.assertEqual(V.corr_distance(1.0), 0.0)
        self.assertEqual(V.corr_distance(-1.0), 1.0)


class TestLinkage(unittest.TestCase):
    def test_two_obvious_groups(self):
        pts = [0.0, 0.1, 0.2, 10.0, 10.1]
        dist = [[abs(a - b) for b in pts] for a in pts]
        merges = V.average_linkage(dist)
        self.assertEqual(len(merges), 4)
        self.assertEqual([m[0] for m in merges], sorted(m[0] for m in merges))
        lab = V.cut(merges, 5, 2)
        self.assertEqual(len(set(lab[:3])), 1)
        self.assertEqual(len(set(lab[3:])), 1)
        self.assertNotEqual(lab[0], lab[3])
        self.assertEqual(V.cut(merges, 5, 5), [0, 1, 2, 3, 4])
        s = V.silhouettes(dist, lab)
        self.assertTrue(all(x > 0.9 for x in s))

    def test_average_linkage_height(self):
        # {0,1} merge at 1; then 2 joins at the AVERAGE of its distances (4, 6)
        dist = [[0, 1, 4], [1, 0, 6], [4, 6, 0]]
        self.assertEqual([m[0] for m in V.average_linkage(dist)], [1, 5.0])


class TestEffectiveTrials(unittest.TestCase):
    def test_recovers_planted_clusters_plus_flat(self):
        rng = random.Random(3)
        bases = [[rng.gauss(0, 1) for _ in range(200)] for _ in range(3)]
        series = []
        for b in bases:
            series += noisy_copies(b, 6, rng)
        series += [[0.0] * 200] * 4
        r = V.effective_trials(series)
        self.assertEqual(r["n"], 22)
        self.assertEqual(r["n_flat"], 4)
        self.assertEqual(r["n_eff"], 4)                     # 3 clusters + the flat one
        for g in range(3):
            self.assertEqual(len(set(r["labels"][g * 6:(g + 1) * 6])), 1)
        self.assertEqual(r["labels"][-4:], [-1] * 4)

    def test_exact_duplicates_are_one_trial(self):
        a = [1.0, -2.0, 0.5, 3.0, -1.0]
        r = V.effective_trials([a[:] for _ in range(10)])
        self.assertEqual(r["n_eff"], 1)
        self.assertEqual(r["n_distinct"], 1)

    def test_independent_series_are_many_trials(self):
        rng = random.Random(5)
        series = [[rng.gauss(0, 1) for _ in range(300)] for _ in range(40)]
        r = V.effective_trials(series)
        self.assertLess(r["mean_abs_corr"], 0.1)
        self.assertGreater(r["n_eff"], 10)

    def test_n_eff_never_exceeds_n(self):
        rng = random.Random(9)
        series = [[rng.gauss(0, 1) for _ in range(50)] for _ in range(12)]
        self.assertLessEqual(V.effective_trials(series)["n_eff"], 12)


class TestBreakeven(unittest.TestCase):
    def test_boundary(self):
        sr, n, skew, kurt, var = 0.3, 200, 0.0, 3.0, 0.01
        k = V.breakeven_trials(sr, n, skew, kurt, var)
        self.assertGreater(k, 1)
        self.assertGreaterEqual(V.deflated_sharpe(sr, n, skew, kurt, k, var)[0], 0.95)
        self.assertLess(V.deflated_sharpe(sr, n, skew, kurt, k + 1, var)[0], 0.95)

    def test_zero_when_psr_fails(self):
        self.assertEqual(V.breakeven_trials(0.05, 100, 0.0, 3.0, 0.1), 0)


def fake_backtest(conn, pair, cfg, start_iso, end_iso, tf_seconds=3600):
    """Trades depend only on cfg['ema_fast'] parity: two families of trials."""
    t0 = int(V.datetime.fromisoformat(start_iso).replace(tzinfo=V.timezone.utc).timestamp())
    odd = cfg["ema_fast"] % 2
    trades = [{"entry_ts": t0 + (2 * d + odd) * DAY, "exit_ts": t0 + (2 * d + odd) * DAY + 3600,
               "pnl": 10.0 * (1 + d % 3), "dir": "long"} for d in range(20)]
    return {"trades": trades, "n_trades": len(trades)}


class TestNeffDB(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        optimizer.ensure_tables(self.conn)
        NE.ensure_tables(self.conn)
        for i in range(8):
            cfg = {**engine.default_cfg(), "ema_fast": 5 + i}
            short = i < 3                      # three trials on a shorter window
            self.conn.execute(
                "INSERT INTO candidates (run_at, pair, tf, params_json, passed, is_start,"
                " split_ts, oos_end) VALUES ('x', 'BTC-USD', '1h', ?, 0, ?, ?, ?)",
                (json.dumps(cfg), "2025-01-01T00:00:00" if short else "2024-01-01T00:00:00",
                 "2025-03-01T00:00:00", "2025-06-01T00:00:00"))
        p = mock.patch.object(NE.engine, "backtest", fake_backtest)
        p.start()
        self.addCleanup(p.stop)

    def test_reference_is_longest_window(self):
        self.assertEqual(NE.reference_window(self.conn, "BTC-USD", "1h"),
                         ("2024-01-01T00:00:00", "2025-03-01T00:00:00"))

    def test_encode_round_trip(self):
        s = [0.0, 0.1, 0.0, -0.2]
        self.assertEqual(NE.decode(NE.encode(s)), s)

    def test_fill_compute_lookup(self):
        self.assertIsNone(NE.lookup(self.conn, "BTC-USD", "1h"))
        ref = NE.reference_window(self.conn, "BTC-USD", "1h")
        self.assertEqual(NE.fill_series(self.conn, "BTC-USD", "1h", ref, progress=False), 8)
        self.assertEqual(NE.fill_series(self.conn, "BTC-USD", "1h", ref, progress=False), 0)
        r = NE.compute(self.conn, "BTC-USD", "1h", ref)
        self.assertEqual(r["n_eff"], 2)        # odd / even ema_fast take identical trades
        got = NE.lookup(self.conn, "BTC-USD", "1h")
        self.assertEqual((got["n_eff"], got["n_series"], got["n_trials"]), (2, 8, 8))

    def test_lookup_without_table(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertIsNone(NE.lookup(conn, "BTC-USD", "1h"))


if __name__ == "__main__":
    unittest.main()
