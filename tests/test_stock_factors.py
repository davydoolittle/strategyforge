"""
Phase 15 evaluator on hand-built data with known answers: signals, filing-date
lag, the worst case, costs, the benchmark and both stop rules.
"""
import math
import os
import sqlite3
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import factor_data as F  # noqa: E402
import stock_factors as SF  # noqa: E402


def trading_days(start, n):
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def make_db(prices, members, facts=(), text=()):
    """prices: {symbol: {day: (open, close)}}; members: [(ticker, start, end, cik, symbol)]."""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE pit_members (ticker TEXT NOT NULL, start TEXT NOT NULL, end TEXT, cik TEXT,
                    cik_source TEXT, name TEXT, PRIMARY KEY (ticker, start))""")
    F.ensure_tables(conn)
    for sym, bars in prices.items():
        conn.executemany("INSERT INTO f15_bars VALUES (?, ?, ?, ?, ?)",
                         [(sym, d, o, c, c) for d, (o, c) in bars.items()])
    for t, s, e, cik, sym in members:
        conn.execute("INSERT INTO pit_members VALUES (?, ?, ?, ?, '', '')", (t, s, e, cik))
        conn.execute("INSERT INTO f15_symbols VALUES (?, ?, ?, ?, 0, 0, 'test')", (t, s, cik, sym))
    conn.executemany("INSERT INTO f15_facts VALUES (?, ?, ?, ?, ?, ?, ?, 'a')", facts)
    conn.executemany("INSERT INTO f15_text VALUES (?, ?, ?, 1, ?, NULL)", text)
    return conn


CAL = trading_days("2016-01-04", 300)


def flat(days, px=100.0):
    return {d: (px, px) for d in days}


class TestSignals(unittest.TestCase):
    def test_price_signals(self):
        px = [100.0 * 1.001 ** k for k in range(SF.LOOK + 1)]
        self.assertAlmostEqual(SF.momentum(px), px[-22] / px[0] - 1)
        self.assertEqual(SF.high52(px), 1.0)
        self.assertAlmostEqual(SF.lowvol(px), 0.0, places=9)               # constant returns: no volatility
        self.assertAlmostEqual(SF.reversal(px), -(px[-1] / px[-22] - 1))
        noisy = [100.0 + (k % 2) for k in range(SF.LOOK + 1)]
        self.assertLess(SF.lowvol(noisy), SF.lowvol(px))                     # noisier ranks lower

    def test_statements_use_the_filing_date_never_the_period_end(self):
        rows = [("revenue:0", "2019-01-01", "2019-12-31", 100.0, "10-K", "2020-02-20"),
                ("cost_of_revenue:0", "2019-01-01", "2019-12-31", 60.0, "10-K", "2020-02-20"),
                ("assets:0", None, "2019-12-31", 200.0, "10-K", "2020-02-20"),
                ("revenue:0", "2020-01-01", "2020-12-31", 999.0, "10-K", "2021-02-20"),
                ("revenue:0", "2019-10-01", "2019-12-31", 30.0, "10-Q", "2020-01-30")]
        facts = SF.index_facts(rows)
        self.assertIsNone(SF.profitability(facts, "2020-02-19"))           # not filed yet
        self.assertAlmostEqual(SF.profitability(facts, "2020-06-30"), 0.2)  # (100 - 60) / 200
        self.assertEqual(SF._annual(facts, "revenue", "2021-03-01"), ("2020-12-31", 999.0))

    def test_preferred_tag_wins_at_the_same_period(self):
        rows = [("revenue:1", "2019-01-01", "2019-12-31", 90.0, "10-K", "2020-02-20"),
                ("revenue:0", "2019-01-01", "2019-12-31", 100.0, "10-K", "2020-02-20")]
        self.assertEqual(SF._annual(SF.index_facts(rows), "revenue", "2020-03-01")[1], 100.0)

    def test_value_parts(self):
        rows = [("net_income:0", "2019-01-01", "2019-12-31", 10.0, "10-K", "2020-02-20"),
                ("equity:0", None, "2019-12-31", 50.0, "10-K", "2020-02-20"),
                ("shares:dei", None, "2020-02-01", 10.0, "10-K", "2020-02-20")]
        ep, bm = SF.value_parts(SF.index_facts(rows), "2020-03-01", 20.0)  # market value 200
        self.assertAlmostEqual((ep, bm), (0.05, 0.25))

    def test_unchanged_10k(self):
        a, b = F.pack({"risk": 2, "revenue": 1}), F.pack({"risk": 2, "revenue": 1})
        text = [("2019-02-20", "a1", a), ("2020-02-20", "a2", b)]
        self.assertAlmostEqual(SF.unchanged_10k(text, "2020-06-30", {}), 1.0)
        self.assertIsNone(SF.unchanged_10k(text, "2020-01-31", {}))          # only one filed by then
        self.assertIsNone(SF.unchanged_10k(text, "2021-03-31", {}))          # latest is over a year old
        self.assertAlmostEqual(SF.cosine({"x": 1}, {"y": 1}), 0.0)


class TestHolding(unittest.TestCase):
    def snap(self, bars):
        return SF.Snapshot(make_db({"SPY": flat(CAL), "A": bars}, [("A", "2010-01-01", None, "c", "A")]))

    def test_open_to_open(self):
        bars = {d: (100.0 + i, 100.0 + i) for i, d in enumerate(CAL)}
        s = self.snap(bars)
        self.assertAlmostEqual(SF.hold_return(s, "A", CAL[10], CAL[20], CAL[9]), 120.0 / 110.0 - 1)

    def test_a_gap_is_not_a_delisting(self):
        bars = flat(CAL)
        del bars[CAL[20]]
        self.assertAlmostEqual(SF.hold_return(self.snap(bars), "A", CAL[10], CAL[20], CAL[9]), 0.0)

    def test_a_series_that_ends_takes_the_worst_case(self):
        s = self.snap(flat(CAL[:15]))
        self.assertAlmostEqual(SF.hold_return(s, "A", CAL[10], CAL[20], CAL[9]), SF.DELIST)
        self.assertAlmostEqual(SF.hold_return(s, "A", CAL[10], CAL[20], CAL[9], worst_case=False), 0.0)

    def test_costs_on_the_weight_traded(self):
        s = SF.Snapshot(make_db({"SPY": flat(CAL), "A": flat(CAL), "B": flat(CAL)},
                                [("A", "2010-01-01", None, "a", "A"), ("B", "2010-01-01", None, "b", "B")]))
        months = [(CAL[20], CAL[21], CAL[42]), (CAL[41], CAL[42], CAL[63]), (CAL[62], CAL[63], CAL[84])]
        r = SF.run_book(s, months, {CAL[20]: ["A"], CAL[41]: ["A"], CAL[62]: ["B"]})
        self.assertAlmostEqual(r[0], -SF.COST)          # bought from cash: 100% traded
        self.assertAlmostEqual(r[1], 0.0)               # same name, flat price: nothing traded
        self.assertAlmostEqual(r[2], -2 * SF.COST)      # A sold, B bought


class TestStops(unittest.TestCase):
    def test_too_many_unpriced_member_months_stops_everything(self):
        cal = trading_days("2016-01-04", 460)            # to April 2017: three formations
        members = [(f"T{i}", "2010-01-01", None, f"c{i}", f"T{i}") for i in range(10)]
        prices = {"SPY": flat(cal), **{f"T{i}": flat(cal) for i in range(9)}}   # T9 never priced
        res = SF.evaluate(SF.Snapshot(make_db(prices, members)), log=lambda m: None)
        self.assertFalse(res["price_passes"])
        self.assertEqual(res["signals"], {})

    def test_thin_statement_data_makes_a_signal_not_testable(self):
        cal = trading_days("2016-01-04", 460)
        members = [(f"T{i}", "2010-01-01", None, f"c{i}", f"T{i}") for i in range(5)]
        prices = {"SPY": flat(cal), **{f"T{i}": {d: (100.0 + i + k * 0.01 * i, 100.0 + i + k * 0.01 * i)
                                                 for k, d in enumerate(cal)} for i in range(5)}}
        res = SF.evaluate(SF.Snapshot(make_db(prices, members)), log=lambda m: None)
        self.assertTrue(res["price_passes"])
        for sig in SF.DATA_SIGNALS:
            self.assertFalse(res["signals"][sig]["testable"])
            self.assertNotIn("psr", res["signals"][sig])
        self.assertIn("psr", res["signals"]["momentum"])

    def test_bonferroni_bar(self):
        self.assertAlmostEqual(SF.MIN_PSR, 0.992857, places=5)


class TestSchedule(unittest.TestCase):
    def test_entry_next_day_exit_at_the_next_entry_and_the_last_at_the_end(self):
        cal = trading_days("2016-01-04", 460)
        s = SF.Snapshot(make_db({"SPY": flat(cal)}, []))
        sch = SF.schedule(s)
        self.assertEqual(sch[0][0], "2017-01-31")
        self.assertEqual(sch[0][1], "2017-02-01")
        self.assertEqual(sch[0][2], sch[1][1])
        self.assertEqual(sch[-1][2], "end")

    def test_top_and_bottom_fifth(self):
        scores = {str(i): float(i) for i in range(10)}
        self.assertEqual(SF.top(scores), ["9", "8"])
        self.assertEqual(SF.bottom(scores), ["0", "1"])


if __name__ == "__main__":
    unittest.main()
