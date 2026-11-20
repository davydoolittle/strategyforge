"""
Tests for Phase 16.2's trend-switching evaluator (app/trend_switch.py): known
answers on hand-built paths through Bull and Bear, the cash yield, costs,
next-open fills, the delay, and the registered constants.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import trend_switch as TS  # noqa: E402


def path(values):
    d0, out = date(2000, 1, 3), []
    d = d0
    while len(out) < len(values):
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out, [values[0]] + values[:-1], values          # open = previous close


NO_CASH = (["1990-01-01"], [0.0])


class TestRegistered(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(TS.MARKETS, ("SPY", "IEF", "GLD", "UUP"))
        self.assertAlmostEqual(TS.MIN_PSR, 0.9875)
        self.assertEqual(TS.SNAPSHOT, "etf-2026-10-30")


class TestSimulate(unittest.TestCase):
    def test_always_bull_equals_hold_less_one_fee(self):
        dates, o, c = path([100 * 1.001 ** i for i in range(400)])
        r = TS.evaluate(dates, o, c, 0.0, NO_CASH)
        self.assertAlmostEqual(r["book"]["equity"][-1], r["bench"]["equity"][-1], places=10)
        self.assertEqual(r["book"]["switches"], 1)
        self.assertTrue(all(r["book"]["in"]))
        self.assertAlmostEqual(max(abs(x) for x in r["excess"]), 0.0, places=12)

    def test_bear_sits_in_cash_earning_the_yield(self):
        dates, o, c = path([400 * 0.999 ** i for i in range(400)])
        cy = (["1990-01-01"], [5.04])                             # 5.04% a year
        r = TS.evaluate(dates, o, c, 0.0, cy)
        self.assertFalse(any(r["book"]["in"]))
        n = len(r["book"]["equity"])
        self.assertAlmostEqual(r["book"]["equity"][-1], (1 + 0.0504 / 252) ** n, places=10)
        self.assertLess(r["bench"]["equity"][-1], 1.0)
        self.assertTrue(all(x > 0 for x in r["excess"]))

    def test_costs_and_next_open(self):
        vals = [100 * 1.001 ** i for i in range(300)]
        dates, o, c = path(vals)
        r = TS.simulate(dates, o, c, 0.001, NO_CASH)
        free = TS.simulate(dates, o, c, 0.0, NO_CASH)
        self.assertAlmostEqual(r["equity"][-1] / free["equity"][-1], 0.999, places=10)
        k0 = r["days"][0] - 1                                     # first decision day
        # bought at the open after the first decision, so the first close is close/open
        self.assertAlmostEqual(free["equity"][0], c[k0 + 1] / o[k0 + 1], places=12)
        late = TS.simulate(dates, o, c, 0.0, NO_CASH, delay=1)
        self.assertFalse(late["in"][0])
        self.assertTrue(late["in"][1])

    def test_switches_on_a_round_trip(self):
        vals = [100 * 1.002 ** i for i in range(300)] + [100 * 1.002 ** 299 * 0.995 ** i for i in range(1, 200)]
        dates, o, c = path(vals)
        r = TS.simulate(dates, o, c, 0.0, NO_CASH)
        self.assertEqual(r["switches"], 2)                         # in, then out when it stops being Bull
        self.assertTrue(r["in"][0])
        self.assertFalse(r["in"][-1])

    def test_yield_lookup_uses_the_usable_date(self):
        cy = (["2020-01-01", "2020-02-01"], [1.0, 2.0])
        self.assertEqual(TS.yield_on(cy, "2019-12-31"), 0.0)
        self.assertEqual(TS.yield_on(cy, "2020-01-31"), 1.0)
        self.assertEqual(TS.yield_on(cy, "2020-02-01"), 2.0)


if __name__ == "__main__":
    unittest.main()
