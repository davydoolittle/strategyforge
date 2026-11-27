"""
Tests for P10.3's calendar-effects evaluator (app/calendar_effects.py): the
registered constants, the day flags, Welch's t and its p, C2's correction, the
halves rule, the repeat row, and the dropped partial months.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import math
import os
import random
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import calendar_effects as CE  # noqa: E402


def weekdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


class TestConstants(unittest.TestCase):
    def test_registered(self):
        self.assertEqual((CE.SNAPSHOT, CE.N_TESTS), ("etf-2026-10-30", 40))
        self.assertAlmostEqual(CE.BAR, 0.00125)
        self.assertEqual(CE.REPEAT, {("SPY", "C1")})


class TestFlags(unittest.TestCase):
    def test_days(self):
        days = weekdays(date(2026, 3, 1), date(2026, 3, 31))     # March 2026: 3rd Friday is the 20th
        f = CE.flags(days)
        on = lambda k: [d.isoformat() for d, x in zip(days, f[k]) if x]   # noqa: E731
        self.assertEqual(on("C1"), ["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-31"])
        self.assertEqual(on("C3"), ["2026-03-16", "2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20"])
        self.assertEqual(on("C4"), ["2026-03-25", "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31"])
        self.assertEqual(on("Mon")[0], "2026-03-02")
        self.assertEqual(CE.third_friday(2026, 5), date(2026, 5, 15))

    def test_partial_months_are_dropped(self):
        days = weekdays(date(2026, 1, 15), date(2026, 4, 10))
        bars = [(d, 100.0 + i) for i, d in enumerate(days)]
        kept, _ = CE.sample(bars)
        self.assertEqual({(d.year, d.month) for d in kept}, {(2026, 2), (2026, 3)})


class TestStats(unittest.TestCase):
    def test_welch_and_tails(self):
        diff, t = CE.welch([1.0, 2.0, 3.0], [0.0, 1.0, 2.0, 3.0])
        self.assertAlmostEqual(diff, 0.5)
        se = math.sqrt(1.0 / 3 + (5 / 3) / 4)
        self.assertAlmostEqual(t, 0.5 / se)
        self.assertAlmostEqual(CE.p_upper(0.0), 0.5)
        self.assertAlmostEqual(CE.p_upper(1.959964), 0.025, places=5)
        self.assertAlmostEqual(CE.p_two(1.959964), 0.05, places=5)
        self.assertEqual(CE.welch([1.0], [1.0, 2.0]), (None, None))

    def test_runs(self):
        self.assertEqual(CE.runs([True, True, False, True, False, False, True]), 3)


class TestSymbol(unittest.TestCase):
    def path(self, bump=0.0):
        rng = random.Random(4)
        days = weekdays(date(2000, 1, 3), date(2012, 12, 31))
        f = CE.flags(days)
        px, bars = 100.0, []
        for d, on in zip(days, f["C4"]):
            px *= 1 + rng.gauss(0.0002, 0.01) + (bump if on else 0.0)
            bars.append((d, px))
        return bars

    def test_a_strong_month_end_passes_and_none_fails(self):
        r = CE.run_symbol("IEF", self.path(bump=0.004), 3.0)["tests"]
        self.assertTrue(r["C4"]["pass"])
        self.assertGreater(r["C4"]["gross_pct_year"], r["C4"]["net_pct_year"])
        z = CE.run_symbol("IEF", self.path(bump=0.0), 3.0)["tests"]
        self.assertFalse(any(z[h]["pass"] for h in ("C1", "C3", "C4")))

    def test_c2_correction_and_repeat(self):
        r = CE.run_symbol("SPY", self.path(), 2.0)["tests"]
        self.assertAlmostEqual(r["C2"]["p"], min(1.0, 5 * r["C2"]["p_raw"]))
        self.assertIn(r["C2"]["weekday"], CE.WEEKDAYS)
        self.assertTrue(r["C1"]["repeat"])
        self.assertFalse(r["C1"]["pass"])


if __name__ == "__main__":
    unittest.main()
