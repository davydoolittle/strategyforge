"""
Tests for the P9.4 tracker's universe (app/tracker.py): the constituents parse,
the stored list with its source, lapsing a leaver without deleting it, and the
instruments rows that the engine refuses to backtest.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import equities as E  # noqa: E402
import tracker as TR  # noqa: E402

PAGE = """<table><tr><th>Foundation</th><td>1983</td></tr></table>
<table><tr><th>Symbol</th><th>Name</th><th>Sector</th></tr>
""" + "".join(f"<tr><td><a>S{i:03d}</a></td><td>Co {i}</td><td>Energy</td></tr>" for i in range(99)) + """
<tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td></tr></table>"""


def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT NOT NULL, ts INTEGER NOT NULL, open REAL, high REAL,"
                 " low REAL, close REAL, volume REAL, PRIMARY KEY (pair, ts))")
    return conn


class TestMembers(unittest.TestCase):
    def test_parse(self):
        m = TR.parse_members(PAGE)
        self.assertEqual(len(m), 100)
        self.assertEqual(m[-1], ("BRK.B", "Berkshire Hathaway", "Financials"))

    def test_store_lapse_and_instruments(self):
        conn = db()
        m = TR.parse_members(PAGE)
        TR.store_members(conn, m, "2026-11-27")
        row = conn.execute("SELECT source, first_seen, active FROM sp100_members WHERE symbol='S000'").fetchone()
        self.assertEqual(row, (TR.SOURCE, "2026-11-27", 1))
        inst = conn.execute("SELECT asset_class, session, spread_bps FROM instruments WHERE symbol='BRK.B'").fetchone()
        self.assertEqual(inst, ("equity", "us_equity", None))            # no cost: the engine refuses a backtest
        added, left = TR.store_members(conn, m[1:] + [("NEW", "New Co", "Energy")], "2026-12-01")
        self.assertEqual((added, left), (["NEW"], ["S000"]))
        self.assertEqual(conn.execute("SELECT active, last_seen FROM sp100_members WHERE symbol='S000'").fetchone(),
                         (0, "2026-11-27"))
        self.assertIn("S000", [r[0] for r in conn.execute("SELECT symbol FROM sp100_members")])   # never deleted
        self.assertNotIn("S000", TR.active(conn))

    def test_a_broken_page_stores_nothing(self):
        conn = db()
        with self.assertRaises(SystemExit):
            TR.store_members(conn, [("A", "A", "X")], "2026-11-27")
        with self.assertRaises(ValueError):
            TR.parse_members("<table><tr><th>Other</th></tr></table>")

    def test_research_sets_are_untouched(self):
        self.assertFalse(set(E.RESEARCH_SYMBOLS) & {"AAPL", "MSFT"})


if __name__ == "__main__":
    unittest.main()
