"""
Tests for the statistics that decide whether a strategy is real.
Run from the repo root:   python -m unittest discover -s tests -v
No database needed: everything here is pure math or synthetic data.
"""

import math
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import validation as V  # noqa: E402
import lineage  # noqa: E402


class TestNormal(unittest.TestCase):
    def test_cdf_known_points(self):
        self.assertAlmostEqual(V.norm_cdf(0.0), 0.5, places=9)
        self.assertAlmostEqual(V.norm_cdf(1.959964), 0.975, places=5)
        self.assertAlmostEqual(V.norm_cdf(-1.959964), 0.025, places=5)

    def test_ppf_inverts_cdf(self):
        for p in (0.001, 0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.99, 0.999):
            self.assertAlmostEqual(V.norm_cdf(V.norm_ppf(p)), p, places=7)


class TestSharpeFamily(unittest.TestCase):
    def test_trade_returns_compound_on_running_equity(self):
        trades = [{"pnl": 100.0}, {"pnl": -50.0}]
        r = V.trade_returns(trades, 1000.0)
        self.assertAlmostEqual(r[0], 0.10)
        self.assertAlmostEqual(r[1], -50.0 / 1100.0)

    def test_sharpe_zero_mean_is_zero(self):
        self.assertEqual(V.sharpe([0.01, -0.01, 0.01, -0.01]), 0.0)

    def test_psr_at_benchmark_is_half(self):
        self.assertAlmostEqual(V.probabilistic_sharpe(0.0, 100, 0.0, 3.0, 0.0), 0.5, places=9)

    def test_psr_grows_with_n(self):
        a = V.probabilistic_sharpe(0.2, 20, 0.0, 3.0)
        b = V.probabilistic_sharpe(0.2, 200, 0.0, 3.0)
        self.assertGreater(b, a)

    def test_psr_negative_skew_hurts(self):
        clean = V.probabilistic_sharpe(0.3, 50, 0.0, 3.0)
        skewed = V.probabilistic_sharpe(0.3, 50, -1.5, 6.0)
        self.assertLess(skewed, clean)

    def test_expected_max_sharpe_known_value(self):
        # BLdP 2014 eq.12 (asymptotic approximation), V=1, hand-computed:
        #   N=10   -> 0.4228*1.2816 + 0.5772*1.7890 = 1.575
        #   N=100  -> 0.4228*2.3263 + 0.5772*2.6790 = 2.530
        #   N=1000 -> 0.4228*3.0902 + 0.5772*3.3743 = 3.254
        # (the exact expected max of N normals is ~2% lower at small N;
        #  the approximation is the published, conservative one)
        self.assertAlmostEqual(V.expected_max_sharpe(10, 1.0), 1.575, delta=0.01)
        self.assertAlmostEqual(V.expected_max_sharpe(100, 1.0), 2.530, delta=0.01)
        self.assertAlmostEqual(V.expected_max_sharpe(1000, 1.0), 3.254, delta=0.01)

    def test_expected_max_scales_with_sqrt_variance(self):
        a = V.expected_max_sharpe(100, 1.0)
        b = V.expected_max_sharpe(100, 4.0)
        self.assertAlmostEqual(b / a, 2.0, places=6)

    def test_dsr_single_trial_equals_psr_vs_zero(self):
        dsr, bench = V.deflated_sharpe(0.3, 50, 0.0, 3.0, 1, 0.5)
        self.assertEqual(bench, 0.0)
        self.assertAlmostEqual(dsr, V.probabilistic_sharpe(0.3, 50, 0.0, 3.0, 0.0))

    def test_dsr_collapses_with_many_trials(self):
        few, _ = V.deflated_sharpe(0.3, 50, 0.0, 3.0, 5, 0.1)
        many, _ = V.deflated_sharpe(0.3, 50, 0.0, 3.0, 5000, 0.1)
        self.assertGreater(few, many)
        self.assertLess(many, 0.5)

    def test_moments_of_normal_sample(self):
        rng = random.Random(1)
        x = [rng.gauss(0, 1) for _ in range(20000)]
        sk, ku = V.moments(x)
        self.assertAlmostEqual(sk, 0.0, delta=0.06)
        self.assertAlmostEqual(ku, 3.0, delta=0.12)


class TestWalkForward(unittest.TestCase):
    def test_folds_do_not_overlap_in_test_and_are_embargoed(self):
        tf = 3600
        lo = 1_700_000_000
        hi = lo + 400 * 86400
        folds = V.make_folds(lo, hi, tf, n_folds=5, train_frac=0.6, embargo_bars=24)
        self.assertEqual(len(folds), 5)
        for f in folds:
            self.assertEqual(f["test_start"] - f["train_end"], 24 * tf)
            self.assertLess(f["train_start"], f["train_end"])
            self.assertLess(f["test_start"], f["test_end"])
            self.assertLessEqual(f["test_end"], hi + 1)
        for a, b in zip(folds, folds[1:]):
            self.assertGreaterEqual(b["test_start"], a["test_end"] - 1)  # tests never overlap
            self.assertGreater(b["train_start"], a["train_start"])       # rolling, not anchored

    def test_folds_respect_warmup(self):
        tf = 3600
        lo = 1_700_000_000
        folds = V.make_folds(lo, lo + 200 * 86400, tf, 3)
        self.assertGreaterEqual(folds[0]["train_start"], lo + 300 * tf)

    def test_too_short_history_gives_no_folds(self):
        self.assertEqual(V.make_folds(0, 100, 3600, 5), [])


class TestMonteCarlo(unittest.TestCase):
    trades = [{"pnl": p} for p in [120, -80, 90, -60, 200, -150, 40, -30, 75, -45]]

    def test_deterministic_for_seed(self):
        a = V.monte_carlo(self.trades, 10000, n_sims=300, seed=3)
        b = V.monte_carlo(self.trades, 10000, n_sims=300, seed=3)
        self.assertEqual(a, b)

    def test_different_seed_differs(self):
        a = V.monte_carlo(self.trades, 10000, n_sims=300, seed=3)
        b = V.monte_carlo(self.trades, 10000, n_sims=300, seed=4)
        self.assertNotEqual(a["dd_p95"], b["dd_p95"])

    def test_dd_percentiles_ordered_and_bounded(self):
        m = V.monte_carlo(self.trades, 10000, n_sims=500)
        self.assertLessEqual(m["dd_p50"], m["dd_p95"])
        self.assertLessEqual(m["dd_p95"], m["dd_p99"])
        worst_possible = sum(-p for p in [-80, -60, -150, -30, -45])
        self.assertLessEqual(m["dd_p99"], worst_possible)

    def test_empty(self):
        self.assertEqual(V.monte_carlo([], 10000)["n_sims"], 0)


class TestDNA(unittest.TestCase):
    def test_costs_do_not_change_dna(self):
        a = {"ema_fast": 9, "commission_pct": 0.1, "initial_capital": 10000}
        b = {"ema_fast": 9, "commission_pct": 0.5, "initial_capital": 50000}
        self.assertEqual(lineage.dna_of(a), lineage.dna_of(b))

    def test_logic_change_changes_dna(self):
        a = {"ema_fast": 9, "use_shorts": True}
        b = {"ema_fast": 9, "use_shorts": False}
        self.assertNotEqual(lineage.dna_of(a), lineage.dna_of(b))

    def test_key_order_irrelevant(self):
        self.assertEqual(lineage.dna_of({"a": 1, "b": 2}), lineage.dna_of({"b": 2, "a": 1}))


if __name__ == "__main__":
    unittest.main()
