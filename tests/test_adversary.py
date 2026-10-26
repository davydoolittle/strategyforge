"""
Tests for the tightened adversary attacks: A4 plateau (+/-10% and +/-25%,
worst neighbour PF) and A7 transfer (verified tick only, PF >= 1.1 on >= 15
trades). engine.backtest is faked so each neighbour's result is known.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import adversary as A  # noqa: E402
import engine  # noqa: E402
import validation as V  # noqa: E402


def result(pf, n=20, net=None):
    return {"pf": pf, "n_trades": n, "net": (100.0 if pf > 1 else -100.0) if net is None else net}


class TestPlateauSteps(unittest.TestCase):
    def run_sens(self, pf_of):
        cfg = engine.default_cfg()
        calls = []

        def fake(conn, pair, c, s, e, tf):
            calls.append(c)
            return result(pf_of(c, cfg))
        with mock.patch.object(V.engine, "backtest", fake):
            return V.param_sensitivity(None, "BTC-USD", cfg, 3600, "a", "b", pct=(0.10, 0.25)), calls

    def test_both_step_sizes_tried(self):
        sens, _ = self.run_sens(lambda c, base: 1.5)
        steps = {r["pct"] for r in sens["neighbours"] if r["param"] == "stop_atr"}
        self.assertEqual(steps, {-0.25, -0.10, 0.10, 0.25})
        self.assertEqual(sens["worst_pf"], 1.5)
        self.assertEqual(sens["plateau_score"], 1.0)

    def test_int_params_not_tried_twice(self):
        sens, _ = self.run_sens(lambda c, base: 1.5)
        for p in ("ema_fast", "ema_mid", "ema_slow"):
            tos = [r["to"] for r in sens["neighbours"] if r["param"] == p]
            self.assertEqual(len(tos), len(set(tos)), p)

    def test_one_bad_far_neighbour_sets_worst_pf(self):
        # only stop_atr at -25% loses money: 70% rule still met, worst-PF rule is not
        def pf_of(c, base):
            return 0.8 if c["stop_atr"] < base["stop_atr"] * 0.8 else 1.4
        sens, _ = self.run_sens(pf_of)
        self.assertGreaterEqual(sens["plateau_score"], A.MIN_PLATEAU)
        self.assertEqual(sens["worst_pf"], 0.8)
        self.assertLess(sens["worst_pf"], A.MIN_WORST_NEIGHBOUR_PF)

    def test_single_step_still_accepted(self):
        cfg = engine.default_cfg()
        with mock.patch.object(V.engine, "backtest", lambda *a: result(1.2)):
            sens = V.param_sensitivity(None, "BTC-USD", cfg, 3600, "a", "b", pct=0.10)
        self.assertEqual({abs(r["pct"]) for r in sens["neighbours"]}, {0.10})


class TestTransfer(unittest.TestCase):
    def run_transfer(self, outcomes, ticks):
        seen = {}

        def fake(conn, pair, cfg, s, e, tf):
            seen[pair] = cfg["tick_size"]
            return outcomes[pair]
        with mock.patch.object(A.engine, "backtest", fake), \
                mock.patch.object(A.W, "compute", lambda *a: ("s", "split", "end")), \
                mock.patch.object(A.collector, "TICK_SIZE", ticks):
            tr, excluded = A.transfer(None, engine.default_cfg(), list(outcomes), 3600, 0, 0)
        return {x["pair"]: x for x in tr}, excluded, seen

    def test_unverified_pair_excluded_and_not_run(self):
        tr, excluded, seen = self.run_transfer(
            {"ETH-USD": result(1.3), "DOGE-USD": result(5.0)}, {"ETH-USD": 0.01})
        self.assertEqual(excluded, ["DOGE-USD"])
        self.assertNotIn("DOGE-USD", seen)
        self.assertEqual(list(tr), ["ETH-USD"])

    def test_runs_at_target_pairs_tick(self):
        _, _, seen = self.run_transfer({"XRP-USD": result(1.3)}, {"XRP-USD": 0.0001})
        self.assertEqual(seen["XRP-USD"], 0.0001)

    def test_hold_needs_pf_and_trades(self):
        tr, _, _ = self.run_transfer({
            "A": result(1.05, n=40),       # profitable but below 1.1
            "B": result(1.5, n=10),        # good PF, too few trades
            "C": result(1.1, n=15),        # exactly on both bars
            "D": result(float("inf"), n=15, net=50.0),
        }, {"A": 0.01, "B": 0.01, "C": 0.01, "D": 0.01})
        self.assertFalse(tr["A"]["holds"])
        self.assertFalse(tr["B"]["holds"])
        self.assertTrue(tr["C"]["holds"])
        self.assertTrue(tr["D"]["holds"])
        self.assertIsNone(tr["D"]["pf"])


class TestTickTable(unittest.TestCase):
    def test_every_traded_pair_has_a_verified_tick(self):
        for p in A.collector.PAIRS:
            self.assertIn(p, A.collector.TICK_SIZE, p)
        self.assertEqual(A.collector.TICK_SIZE["XRP-USD"], 0.0001)


if __name__ == "__main__":
    unittest.main()
