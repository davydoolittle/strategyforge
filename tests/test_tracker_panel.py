"""
Tests for the dashboard's Tracker panel (web.tracker_panel, /api/tracker): the
rows, percentiles and who added each name, the footer, an explanation for every
metric, and that no string it shows reads as a trade call.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fundamentals as FU  # noqa: E402
import watchlist as WL  # noqa: E402
import web  # noqa: E402
from test_market_context import TRADE_CALL  # noqa: E402

HERE = os.path.dirname(__file__)
EXPLAIN = os.path.join(HERE, "..", "app", "static", "explain.json")
PAGE = os.path.join(HERE, "..", "app", "static", "index.html")


def strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield str(k)
            yield from strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from strings(v)


def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE instruments (symbol TEXT PRIMARY KEY, asset_class TEXT, session TEXT)")
    conn.executemany("INSERT INTO instruments VALUES (?, 'equity', 'us_equity')", [("AAPL",), ("MU",)])
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, close REAL, session TEXT)")
    for i in range(260):
        conn.execute("INSERT INTO candles VALUES ('AAPL', ?, ?, 'us_equity')", (1_700_000_000 + i * 86400, 100.0 + i))
    WL.ensure_watch_table(conn)
    FU.ensure_tables(conn)
    WL.add(conn, "AAPL", notes="margins", today="2026-11-27")
    WL.add(conn, "AAPL", "momentum", {"score": 2.0, "ret_12m": 0.4}, today="2026-11-27")
    WL.add(conn, "MU", "value_quality", {"piotroski_f": 8, "pe": 8.1}, today="2026-11-27")
    m = {"symbol": "AAPL", "name": "Apple Inc.", "sic_desc": "Computers", "pe": 39.6, "piotroski_f": 9,
         "pct_all": {"pe": 75.0}, "pct_sector": {"pe": 50.0}, "sector_peers": 4, "facts_through": "2026-09-27"}
    conn.execute("INSERT INTO fund_metrics VALUES ('2026-11-27', 'AAPL', ?)", (json.dumps(m),))
    return conn


class TestPanel(unittest.TestCase):
    def test_rows(self):
        p = web.tracker_panel(db())
        self.assertEqual(p["footer"], "Facts and traditional readings, not recommendations. Nothing here is a validated signal.")
        rows = {r["ticker"]: r for r in p["rows"]}
        a = rows["AAPL"]
        self.assertEqual(a["price"], 359.0)
        self.assertAlmostEqual(a["chg_1d"], 359.0 / 358.0 - 1)
        self.assertAlmostEqual(a["chg_12m"], 359.0 / 107.0 - 1)
        self.assertEqual(a["ratios"]["pe"], {"value": 39.6, "pct_sp100": 75.0, "pct_sector": 50.0})
        self.assertEqual(sorted(x["added_by"] for x in a["added"]), ["momentum", "user"])
        self.assertIsNone(rows["MU"]["price"])                          # no bars yet: a dash, not an error
        self.assertEqual(p["not_available"], "not available (paid estimates)")

    def test_no_trade_calls_anywhere(self):
        p = web.tracker_panel(db())
        for s in strings(p):
            self.assertIsNone(TRADE_CALL.search(s), s)
        with open(PAGE) as f:
            visible = re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", f.read(), flags=re.S)
        self.assertIsNone(TRADE_CALL.search(visible))

    def test_every_metric_has_an_explanation(self):
        with open(EXPLAIN) as f:
            e = json.load(f)
        need = {"t_" + k for k in web.TRACKER_RATIOS} | {"t_piotroski_f", "t_paid_estimates"}
        import screens as SN
        need |= {"t_screen_" + k for k in SN.SCREENS}
        self.assertEqual(need - set(e), set())

    def test_no_watchlist_table(self):
        conn = sqlite3.connect(":memory:")
        self.assertIn("no watchlist yet", web.tracker_panel(conn)["missing"])


if __name__ == "__main__":
    unittest.main()
