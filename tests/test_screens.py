"""
Tests for the P9.4 tracker screens (app/screens.py): the declared thresholds,
momentum's decile and reversal exclusion, value+quality, the insider window by
transaction date, the cap, lapsing, and that user rows are never touched.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import math
import os
import sqlite3
import sys
import unittest
from datetime import date
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import screens as SN  # noqa: E402
import watchlist as WL  # noqa: E402

DAY = 86400


def db(symbols):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL,"
                 " volume REAL, session TEXT, PRIMARY KEY (pair, ts))")
    conn.execute("CREATE TABLE instruments (symbol TEXT PRIMARY KEY, asset_class TEXT, session TEXT)")
    conn.executemany("INSERT INTO instruments VALUES (?, 'equity', 'us_equity')", [(s,) for s in symbols])
    WL.ensure_watch_table(conn)
    return conn


def add_path(conn, sym, daily, last_month=None, n=300):
    lp = math.log(100)
    for i in range(n):
        step = daily if (last_month is None or i < n - 21) else last_month
        lp += math.log(step) + (0.01 if i % 2 else -0.01)
        conn.execute("INSERT INTO candles VALUES (?, ?, 1, 1, 1, ?, 1, 'us_equity')",
                     (sym, 1_600_000_000 + i * DAY, math.exp(lp)))


class TestMomentum(unittest.TestCase):
    def test_decile_and_reversal_exclusion(self):
        syms = [f"S{i:02d}" for i in range(30)]
        conn = db(syms)
        hot = {"S01", "S28", "S29"}                          # the three hottest last months
        for i, s in enumerate(syms):
            add_path(conn, s, 1.0012 - 0.00002 * i, last_month=1.0015 if s in hot else 0.999)
        picks = SN.momentum(conn, syms)
        names = [s for s, _ in picks]
        self.assertEqual(len(picks) + 1, round(30 * SN.MOMENTUM_DECILE))    # decile of 30 is 3; one is hot
        self.assertNotIn("S01", names)                       # in the top decile, but a hot month: excluded
        self.assertEqual(names, ["S00", "S02"])


class TestValueQuality(unittest.TestCase):
    def test_rule(self):
        m = {"A": {"piotroski_f": 8, "pe": 10.0, "fcf_yield": 0.05, "sector2": "35", "sic_desc": "x"},
             "B": {"piotroski_f": 8, "pe": 45.0, "fcf_yield": 0.05, "sector2": "35"},      # above the median 42.5
             "C": {"piotroski_f": 6, "pe": 40.0, "fcf_yield": 0.05, "sector2": "35"},      # F too low
             "D": {"piotroski_f": 9, "pe": 50.0, "fcf_yield": -0.01, "sector2": "35"},     # negative FCF
             "E": {"piotroski_f": 9, "pe": 6.0, "fcf_yield": 0.02, "sector2": "99"}}       # alone in its sector
        self.assertEqual([s for s, _ in SN.value_quality(m)], ["A"])


class TestInsider(unittest.TestCase):
    def setUp(self):
        self.conn = db(["BA", "BRK.B", "X"])
        self.conn.execute("CREATE TABLE insider_txns (ticker TEXT, owner_name TEXT, code TEXT, value REAL,"
                          " txn_date TEXT, filing_date TEXT)")

    def add(self, t, who, d, code="P", filed=None):
        self.conn.execute("INSERT INTO insider_txns VALUES (?, ?, ?, 100000, ?, ?)", (t, who, code, d, filed or d))

    def test_three_buys_two_people_thirty_days_by_transaction_date(self):
        for who, d in (("a", "2026-11-01"), ("b", "2026-11-10"), ("a", "2026-11-20")):
            self.add("BRK-B", who, d)                         # dash in the feed, dot in instruments
        for who, d in (("a", "2026-11-01"), ("a", "2026-11-05"), ("a", "2026-11-09")):
            self.add("BA", who, d)                            # one insider only
        self.add("X", "a", "2026-10-01", filed="2026-11-20")  # filed recently, traded too long ago
        self.add("X", "b", "2026-11-15")
        self.add("X", "c", "2026-11-16", code="S")            # a sale is not a purchase
        picks = SN.insider_cluster(self.conn, ["BA", "BRK.B", "X"], date(2026, 11, 27))
        self.assertEqual([s for s, _ in picks], ["BRK.B"])
        self.assertEqual((picks[0][1]["buys"], picks[0][1]["insiders"]), (3, 2))


class TestApply(unittest.TestCase):
    def test_lapse_cap_and_user_rows(self):
        conn = db(["A", "B", "C"])
        WL.add(conn, "A", today="2026-11-01")                                  # the owner's row
        SN.apply(conn, "momentum", [("A", {"score": 1}), ("B", {"score": 2})], "2026-11-26")
        added, lapsed = SN.apply(conn, "momentum", [("C", {"score": 3})], "2026-11-27")
        self.assertEqual((added, sorted(lapsed)), (["C"], ["A", "B"]))
        st = {(r["ticker"], r["added_by"]): r["status"] for r in WL.rows(conn, include_inactive=True)}
        self.assertEqual(st[("A", "user")], "active")                          # untouched
        self.assertEqual((st[("A", "momentum")], st[("B", "momentum")], st[("C", "momentum")]),
                         ("lapsed", "lapsed", "active"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0], 4)   # nothing deleted

    def test_cap_and_declared_text(self):
        self.assertEqual(SN.CAP, 10)
        self.assertEqual(set(SN.SCREENS), {"momentum", "value_quality", "insider_cluster"})
        many = {f"S{i}": {"piotroski_f": 9, "pe": float(i), "fcf_yield": 0.05, "sector2": "35"} for i in range(30)}
        self.assertLessEqual(len(SN.value_quality(many)), SN.CAP)


if __name__ == "__main__":
    unittest.main()
