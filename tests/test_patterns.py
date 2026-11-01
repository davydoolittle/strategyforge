"""
The pattern ledger's statistics, on synthetic markets with planted answers.
"""
import os
import random
import sqlite3
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import patterns as P  # noqa: E402


class FakeMarket:
    def __init__(self, n, px=None, start=date(2000, 1, 3)):
        self.days, d = [], start
        while len(self.days) < n:
            if d.weekday() < 5:
                self.days.append(d)
            d += timedelta(days=1)
        self.iso = [x.isoformat() for x in self.days]
        self.n = n
        self.px = px or {}
        self.macro = {}


def pattern(on, h=10, sign=1):
    return {"pid": "x", "family": "t", "claim": "c", "asset": "A", "h": h, "sign": sign,
            "signal": lambda m: on}


def planted(boost, n=3000, seed=1, h=10, every=40):
    """A random walk with an extra `boost` over the h days after each signal."""
    rng = random.Random(seed)
    on = [i % every == 0 for i in range(n)]
    px, x, left = [], 100.0, 0
    for i in range(n):
        px.append(x)
        if on[i]:
            left = h
        x *= 1 + rng.gauss(0.0003, 0.008) + (boost / h if left else 0)
        left = max(left - 1, 0)
    return FakeMarket(n, {"A": px}), on


class TestHelpers(unittest.TestCase):
    def test_sma_change_diff(self):
        self.assertEqual(P.sma([1.0, 2.0, 3.0, 4.0], 2), [None, 1.5, 2.5, 3.5])
        self.assertEqual(P.change([1.0, 2.0], 1), [None, 1.0])
        self.assertEqual(P.diff([1.0, None, 3.0], 1), [None, None, None])

    def test_sma_skips_the_years_before_a_fund_existed(self):
        self.assertEqual(P.sma([None, None, 2.0, 4.0], 2), [None, None, None, 3.0])

    def test_drawdown_and_rising_edge(self):
        self.assertEqual(P.drawdown([1.0, 2.0, 1.0]), [0.0, 0.0, -0.5])
        self.assertEqual(P.rising_edge([False, True, True, False, True]),
                         [False, True, False, False, True])

    def test_rsi_extremes(self):
        self.assertEqual(P.rsi([float(i) for i in range(1, 40)])[-1], 100.0)
        self.assertLess(P.rsi([float(40 - i) for i in range(39)])[-1], 1.0)

    def test_spaced_windows_never_overlap(self):
        self.assertEqual(P.spaced([0, 1, 5, 10, 11, 20], 10), [0, 10, 20])

    def test_election_eve_is_the_day_before_the_tuesday(self):
        m = FakeMarket(300, start=date(2022, 6, 1))
        on = P.election_eve(m, [2022])
        self.assertEqual(m.days[on.index(True)], date(2022, 11, 7))

    def test_first_fed_move_after_a_quiet_year(self):
        m = FakeMarket(800)
        t = [5.0] * 800
        for i in range(400, 800):
            t[i] = 5.25
        for i in range(420, 800):
            t[i] = 5.5
        m.macro["fed_target"] = t
        on = P.fed_first_moves(m, 1)
        self.assertEqual([i for i, v in enumerate(on) if v], [400])

    def test_every_pattern_has_a_unique_id_and_a_sign(self):
        reg = P.registry()
        self.assertEqual(len({p["pid"] for p in reg}), len(reg))
        self.assertTrue(all(p["sign"] in (1, -1) for p in reg))


class TestEvaluate(unittest.TestCase):
    def test_a_real_effect_is_found(self):
        m, on = planted(0.04)
        r = P.evaluate(m, pattern(on))
        self.assertGreater(r["mean_on"] - r["mean_base"], 0.02)
        self.assertLess(r["p"], 0.001)

    def test_noise_is_not_found(self):
        m, on = planted(0.0)
        self.assertGreater(P.evaluate(m, pattern(on))["p"], 0.05)

    def test_a_signal_on_the_last_days_has_no_window_but_shows_active(self):
        m, _ = planted(0.0, n=500)
        on = [False] * 498 + [True, True]
        r = P.evaluate(m, pattern(on))
        self.assertEqual(r["n"], 0)
        self.assertTrue(r["active"])
        self.assertEqual(r["last_on"], m.iso[-1])

    def test_an_event_stays_active_for_its_window(self):
        m, _ = planted(0.0, n=500)
        on = [False] * 500
        on[495] = True
        self.assertTrue(P.evaluate(m, pattern(on, h=10))["active"])
        on = [False] * 500
        on[480] = True
        self.assertFalse(P.evaluate(m, pattern(on, h=10))["active"])

    def test_a_condition_is_active_only_while_it_holds(self):
        m, _ = planted(0.0, n=500)
        on = [False] * 500
        on[495] = True
        p = {**pattern(on, h=10), "pid": "dollar_gold"}
        self.assertFalse(P.evaluate(m, p)["active"])
        on[499] = True
        self.assertTrue(P.evaluate(m, {**p, "signal": lambda m: on})["active"])

    def test_every_condition_is_a_real_pattern(self):
        self.assertLessEqual(P.CONDITIONS, {p["pid"] for p in P.registry()})

    def test_a_spread_is_a_minus_b(self):
        m = FakeMarket(3, {"A": [1.0, 1.0, 1.2], "B": [1.0, 1.0, 1.1]})
        self.assertAlmostEqual(P.forward(m, ("A", "B"), 0, 2), 0.1)

    def test_no_price_means_no_window(self):
        m = FakeMarket(3, {"A": [None, 1.0, 1.2]})
        self.assertIsNone(P.forward(m, "A", 0, 1))


class TestLabels(unittest.TestCase):
    def r(self, p, n=20, first=0.01, second=0.01):
        return {"n": n, "n_base": 50, "p": p, "first_half": first, "second_half": second}

    def test_benjamini_hochberg(self):
        ps = {"a": 0.001, "b": 0.02, "c": 0.5, "d": 0.9}
        self.assertEqual(P.benjamini_hochberg(ps, 0.10), {"a", "b"})

    def test_labels(self):
        res = {"good": self.r(0.0001), "maybe": self.r(0.06), "wrong_half": self.r(0.0001, second=-0.01),
               "backwards": self.r(0.99999), "thin": self.r(0.0001, n=7), "flat": self.r(0.5)}
        res.update({f"f{i}": self.r(0.5) for i in range(10)})
        got = P.label(res, {k: 1 for k in res})
        self.assertEqual(got["good"], "SUPPORTED")
        self.assertEqual(got["maybe"], "SUGGESTIVE")
        self.assertEqual(got["wrong_half"], "NOT SUPPORTED")
        self.assertEqual(got["backwards"], "OPPOSITE")
        self.assertEqual(got["thin"], "TOO FEW")
        self.assertEqual(got["flat"], "NOT SUPPORTED")

    def test_a_short_claim_needs_negative_halves(self):
        res = {"s": self.r(0.0001, first=-0.01, second=-0.01)}
        res.update({f"f{i}": self.r(0.5) for i in range(5)})
        self.assertEqual(P.label(res, {k: -1 for k in res})["s"], "SUPPORTED")


class TestNoteworthy(unittest.TestCase):
    def test_no_ledger_means_no_lines(self):
        self.assertEqual(P.noteworthy(sqlite3.connect(":memory:")), [])

    def test_too_few_never_shows_and_the_evidence_is_stated(self):
        c = sqlite3.connect(":memory:")
        P.ensure_tables(c)
        row = "INSERT INTO pattern_results VALUES (?, 'f', ?, 'SPY', 63, 1, ?, 9, 90, .04, .02, .7, .6, .1, .1, .1, 'x', ?, 'x', 'x')"
        c.execute(row, ("a", "Claim A", "SUPPORTED", 1))
        c.execute(row, ("b", "Claim B", "TOO FEW", 1))
        c.execute(row, ("c", "Claim C", "NOT SUPPORTED", 0))
        lines = P.noteworthy(c)
        self.assertEqual(len(lines), 2)
        self.assertIn("[SUPPORTED] Claim A: SPY next 63 days +4.0% vs +2.0%", lines[0])
        self.assertIn("not a recommendation", lines[1])


if __name__ == "__main__":
    unittest.main()
