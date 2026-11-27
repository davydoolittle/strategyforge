"""
Tests for P10.4's Treasury month-end evaluator (app/treasury_month_end.py):
the registered constants, the on-days, entry at the open and exit at the close
with costs, cash yield, the delayed entry, and full months only.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import treasury_month_end as TM  # noqa: E402

NO_CASH = (["1990-01-01"], [0.0])


def weekdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class TestRule(unittest.TestCase):
    def test_constants(self):
        self.assertEqual((TM.FUNDS, TM.ON_DAYS, TM.SNAPSHOT), (("IEF", "TLT"), 5, "etf-2026-10-30"))
        self.assertAlmostEqual(TM.MIN_PSR, 0.975)

    def test_on_days(self):
        d = weekdays(date(2026, 3, 1), date(2026, 3, 31))
        on = [x for x, f in zip(d, TM.on_flags(d)) if f]
        self.assertEqual(on, ["2026-03-25", "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31"])
        late = [x for x, f in zip(d, TM.on_flags(d, skip_first=1)) if f]
        self.assertEqual(late, on[1:])

    def test_entry_open_exit_close_and_costs(self):
        d = weekdays(date(2026, 3, 1), date(2026, 4, 30))
        opens = [100.0] * len(d)
        closes = [100.0] * len(d)
        i = d.index("2026-03-25")
        closes[i] = 101.0                                   # the first on-day: open 100 -> close 101
        for k in range(i + 1, i + 5):
            opens[k], closes[k] = 101.0, 101.0
        on = TM.on_flags(d)
        r = TM.simulate(d, opens, closes, 0.001, NO_CASH, on)
        self.assertAlmostEqual(r["equity"][i], 0.999 * 1.01)
        self.assertAlmostEqual(r["equity"][i + 4], 0.999 * 1.01 * 0.999)   # sold at the last close
        self.assertTrue(r["in"][i] and r["in"][i + 4] and not r["in"][i + 5])
        hold = TM.simulate(d, opens, closes, 0.001, NO_CASH, on, hold_always=True)
        self.assertAlmostEqual(hold["equity"][0], 0.999)    # one entry fee, then held

    def test_cash_earns_the_yield(self):
        d = weekdays(date(2026, 3, 1), date(2026, 3, 31))
        flat = [100.0] * len(d)
        r = TM.simulate(d, flat, flat, 0.0, (["1990-01-01"], [5.04]), [False] * len(d))
        self.assertAlmostEqual(r["equity"][-1], (1 + 0.0504 / 252) ** len(d))

    def test_a_strong_month_end_beats_cash(self):
        d = weekdays(date(2010, 1, 1), date(2015, 12, 31))
        on = TM.on_flags(d)
        px, opens, closes = 100.0, [], []
        for f in on:
            opens.append(px)
            px *= 1.003 if f else 1.0
            closes.append(px)
        r = TM.evaluate(d, opens, closes, 0.0001, NO_CASH)
        self.assertGreater(r["vs_cash"]["psr"], 0.99)
        self.assertLess(abs(TM.summary(r["rule"])["cagr"] - TM.summary(r["hold"])["cagr"]), 0.01)


if __name__ == "__main__":
    unittest.main()
