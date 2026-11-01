"""
The long-term watchlist. No network: fetches are stubbed.
"""
import os
import sqlite3
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import watchlist as WL  # noqa: E402

D0 = 1704067200    # Monday 2024-01-01
DAY = 86400
YEAR = int(WL.YEAR_S)


def fetch(sym, end, headers):
    return [(D0 + i * DAY, 10.0, 20.0, 9.0, 10.0 + i, 1.0) for i in range(5)]


class TestWatchlist(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, close REAL)")
        WL.ensure_tables(self.conn)

    def test_refresh_replaces_rows_and_never_touches_candles(self):
        for _ in range(2):
            WL.refresh(self.conn, {}, date(2026, 11, 1), fetch)
        n = self.conn.execute("SELECT COUNT(*) FROM watchlist_bars").fetchone()[0]
        self.assertEqual(n, 5 * len(WL.WATCHLIST))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0], 0)

    def test_a_failed_fetch_keeps_the_old_series(self):
        WL.refresh(self.conn, {}, date(2026, 11, 1), fetch)

        def bad(sym, end, headers):
            return []
        with self.assertRaises(ValueError):
            WL.refresh(self.conn, {}, date(2026, 11, 1), bad)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM watchlist_bars WHERE symbol='VOO'"
                                           ).fetchone()[0], 5)

    def test_stats(self):
        closes = [(0, 100.0), (YEAR // 2, 50.0), (YEAR, 110.0), (2 * YEAR, 121.0)]
        s = WL.stats(closes)
        self.assertAlmostEqual(s["y1"], 0.10)
        self.assertIsNone(s["y3"])
        self.assertAlmostEqual(s["max_dd"], -0.5)

    def test_the_research_universe_is_separate(self):
        import equities as EQ
        self.assertEqual(set(WL.WATCHLIST) & set(EQ.UNIVERSE), set())


if __name__ == "__main__":
    unittest.main()
