"""
Tests for Phase 14's cross-sectional momentum evaluator (app/xsmom.py) and its
data wiring in equities.py / etf_universe.py: the registered rule, 12-1
momentum, selection and ties, known-answer returns on hand-built prices, costs,
the delay, the calendar check, and that research symbols keep their vendor.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import math
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import equities as E  # noqa: E402
import etf_portfolio as P  # noqa: E402
import etf_universe as U  # noqa: E402
import xsmom as X  # noqa: E402


def calendar(start=datetime(2000, 1, 3, tzinfo=timezone.utc), months=16):
    """Weekdays for `months` months."""
    out, d = [], start
    end_month = (start.year * 12 + start.month - 1 + months)
    while d.year * 12 + d.month - 1 < end_month:
        if d.weekday() < 5:
            out.append(int(d.timestamp()))
        d += timedelta(days=1)
    return out


def path(cal, daily):
    """Closes growing by a constant daily factor; opens equal the previous close,
    so a holding earns exactly the close-to-close return."""
    closes = [100 * daily ** i for i in range(len(cal))]
    opens = [100.0] + closes[:-1]
    return opens, closes


class TestRegisteredRule(unittest.TestCase):
    def test_constants_are_the_registration(self):
        self.assertEqual(X.SLEEVES[0][1], ("XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"))
        self.assertEqual(len(X.SLEEVES[1][1]), 17)
        self.assertEqual([(s[0], s[2], s[3]) for s in X.SLEEVES], [("sectors", 0.5, 3), ("countries", 0.5, 6)])
        self.assertEqual((X.LOOKBACK_MONTHS, X.SKIP_MONTHS, X.MIN_PSR, X.START), (12, 1, 0.95, "1998-12-22"))
        self.assertEqual(set(E.XSMOM_SPREAD_BPS.values()), {5.0, 15.0})
        self.assertEqual({E.XSMOM_SPREAD_BPS[s] for s in E.XSMOM_SECTORS}, {5.0})
        for s in ("XLRE", "XLC", "EWZ", "EWY", "EWT"):            # outside the cohort
            self.assertNotIn(s, E.XSMOM_UNIVERSE)
        self.assertTrue(set(E.UNIVERSE) <= set(E.RESEARCH_SYMBOLS))
        self.assertTrue(set(E.XSMOM_UNIVERSE) <= set(E.RESEARCH_SYMBOLS))


class TestMomentumAndSelection(unittest.TestCase):
    def setUp(self):
        self.cal = calendar(months=16)
        self.ends = P.month_ends(self.cal)

    def test_12_1_skips_the_latest_month(self):
        far, near, sig = self.ends[0], self.ends[11], self.ends[12]
        n = len(self.cal)
        # A wins over months 1-11, then crashes in month 12; B is steady.
        a = [100.0 * (1.01 ** i if i <= near else 1.01 ** near * 0.01) for i in range(n)]
        b = [100.0 * 1.001 ** i for i in range(n)]
        closes = {"A": a, "B": b}
        self.assertEqual(X.rank(closes, ["A", "B"], far, near), ["A", "B"])
        self.assertLess(a[sig] / a[far], b[sig] / b[far])         # A is worse over 12-0

    def test_ties_by_symbol_and_bottom_pick(self):
        n = len(self.cal)
        closes = {s: [100.0 * 1.001 ** i for i in range(n)] for s in ("C", "A", "B")}
        self.assertEqual(X.rank(closes, ["C", "A", "B"], 0, 200), ["A", "B", "C"])
        sleeves = (("s", ("C", "A", "B"), 1.0, 1),)
        t = X.make_targets(self.cal, sleeves, "top")(closes, self.ends[12], self.ends[0])
        self.assertEqual(t, {"A": 1.0})
        t = X.make_targets(self.cal, sleeves, "bottom")(closes, self.ends[12], self.ends[0])
        self.assertEqual(t, {"C": 1.0})
        t = X.make_targets(self.cal, sleeves, "all")(closes, self.ends[12], self.ends[0])
        self.assertEqual({k: round(v, 12) for k, v in t.items()}, {"A": 0.333333333333, "B": 0.333333333333,
                                                                    "C": 0.333333333333})

    def test_weights_sum_to_one_across_sleeves(self):
        n = len(self.cal)
        closes = {s: [100.0 * (1 + 0.0001 * k) ** i for i in range(n)] for k, s in enumerate("ABCDEF")}
        sleeves = (("x", ("A", "B", "C"), 0.5, 1), ("y", ("D", "E", "F"), 0.5, 2))
        t = X.make_targets(self.cal, sleeves)(closes, self.ends[12], self.ends[0])
        self.assertEqual(t, {"C": 0.5, "F": 0.25, "E": 0.25})


class TestKnownAnswer(unittest.TestCase):
    """Three assets, one sleeve holding the best one, no costs."""

    def setUp(self):
        self.cal = calendar(months=16)
        self.opens, self.closes = {}, {}
        for s, g in (("A", 1.0010), ("B", 1.0005), ("C", 1.0000)):
            self.opens[s], self.closes[s] = path(self.cal, g)
        self.rate = {s: 0.0 for s in "ABC"}
        self.sleeves = (("s", ("A", "B", "C"), 1.0, 1),)

    def test_book_holds_the_winner_and_benchmark_the_average(self):
        r = X.evaluate(self.cal, self.opens, self.closes, self.rate, sleeves=self.sleeves)
        ends = P.month_ends(self.cal)
        first_fill = ends[12] + 1
        self.assertEqual(r["book"]["days"][0], first_fill)
        # book = A held from the first fill's open
        for k, d in enumerate(r["book"]["days"]):
            self.assertAlmostEqual(r["book"]["equity"][k], 1.0010 ** (d - first_fill + 1), places=12)
        # benchmark rebalanced monthly to 1/3 each: each month the mean of the three returns
        bench_months = [m for m in ends if m >= first_fill]
        prev = first_fill - 1
        for m, ret in zip(bench_months, r["rm"]):
            days = m - prev
            want = sum(g ** days for g in (1.0010, 1.0005, 1.0)) / 3 - 1
            self.assertAlmostEqual(ret, want, places=12)
            prev = m
        self.assertTrue(all(x > 0 for x in r["excess"]))
        self.assertEqual(len(r["excess"]), len(r["months"]))

    def test_costs_charge_half_spread_on_the_first_fill(self):
        rate = {s: 15.0 * 0.5 / 10000 for s in "ABC"}
        r = X.evaluate(self.cal, self.opens, self.closes, rate, sleeves=self.sleeves)
        free = X.evaluate(self.cal, self.opens, self.closes, self.rate, sleeves=self.sleeves)
        # fee paid at the open, out of cash; equity is measured at that day's close
        self.assertAlmostEqual(r["book"]["equity"][0] / free["book"]["equity"][0], 1 - 0.00075 / 1.0010, places=12)
        doubled = X.evaluate(self.cal, self.opens, self.closes, rate, sleeves=self.sleeves, cost_x=2.0)
        self.assertAlmostEqual(doubled["book"]["equity"][0] / free["book"]["equity"][0], 1 - 0.0015 / 1.0010, places=12)

    def test_delay_moves_the_fill_one_day(self):
        r = X.evaluate(self.cal, self.opens, self.closes, self.rate, sleeves=self.sleeves, delay=1)
        self.assertEqual(r["book"]["days"][0], P.month_ends(self.cal)[12] + 2)

    def test_selections_and_turnover(self):
        sel = X.selections(self.cal, self.closes, self.sleeves)
        self.assertTrue(all(s["s"] == ["A"] for _, s in sel))
        self.assertEqual(X.turnover(sel, self.sleeves), 0.0)
        flip = [(0, {"s": ["A"]}), (1, {"s": ["B"]}), (2, {"s": ["B"]})]
        self.assertEqual(X.turnover(flip, self.sleeves), 0.5)

    def test_halves(self):
        self.assertEqual(X.split(["2012-11-30", "2012-12-31", "2013-01-31"], [1, 2, 3], None, "2013-01-01"), [1, 2])
        self.assertEqual(X.split(["2012-12-31", "2013-01-31"], [2, 3], "2013-01-01", None), [3])


class TestLoad(unittest.TestCase):
    def db(self, drop=None):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE candles (pair TEXT NOT NULL, ts INTEGER NOT NULL, open REAL, high REAL,"
                     " low REAL, close REAL, volume REAL, PRIMARY KEY (pair, ts))")
        E.ensure_tables(conn)
        E.seed_xsmom(conn)
        cal = calendar(datetime(1998, 12, 22, tzinfo=timezone.utc), months=3)
        for s in ("XLK", "EWJ"):
            for t in cal:
                if (s, t) != drop:
                    conn.execute("INSERT INTO bar_snapshots VALUES ('snap', ?, ?, 1, 1, 1, 1, 1)", (s, t))
        return conn, cal

    def test_loads_costs_from_instruments(self):
        conn, cal = self.db()
        got_cal, opens, closes, rate = X.load(conn, "snap", symbols=["XLK", "EWJ"])
        self.assertEqual(got_cal, cal)
        self.assertAlmostEqual(rate["XLK"], 5.0 * 0.5 / 1e4)
        self.assertAlmostEqual(rate["EWJ"], 15.0 * 0.5 / 1e4)

    def test_a_missing_bar_is_refused(self):
        conn, cal = self.db()
        conn2, _ = self.db(drop=("EWJ", cal[5]))
        with self.assertRaises(ValueError):
            X.load(conn2, "snap", symbols=["XLK", "EWJ"])
        with self.assertRaises(ValueError):
            X.load(conn, "nosnap", symbols=["XLK", "EWJ"])


class TestDataWiring(unittest.TestCase):
    def test_seed_fills_spread_on_existing_screen_rows(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE candles (pair TEXT NOT NULL, ts INTEGER NOT NULL, open REAL, high REAL,"
                     " low REAL, close REAL, volume REAL, PRIMARY KEY (pair, ts))")
        E.ensure_tables(conn)
        conn.execute("INSERT INTO instruments (symbol, asset_class, session, source) VALUES"
                     " ('XLE', 'etf', 'us_equity', 'alpaca')")
        E.seed_xsmom(conn)
        self.assertEqual(conn.execute("SELECT spread_bps, listed_from, source FROM instruments"
                                      " WHERE symbol='XLE'").fetchone(), (5.0, "1998-12-22", "alpaca"))
        self.assertEqual(conn.execute("SELECT spread_bps FROM instruments WHERE symbol='EWU'").fetchone(), (15.0,))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0], 26)

    def test_screen_never_resources_research_symbols(self):
        conn = sqlite3.connect(":memory:")
        with mock.patch.object(U.E, "backfill", return_value=[]) as bf:
            U.fetch_member_bars(conn, ["XLE", "SPY", "EWJ", "SGOV"], {})
        self.assertEqual(bf.call_args.kwargs["symbols"], ["SGOV"])


if __name__ == "__main__":
    unittest.main()
