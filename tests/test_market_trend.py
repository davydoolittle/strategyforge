"""
Tests for the dashboard's market trend strip (app/market_trend.py): the fixed
200-day rule, crypto's daily close, the card summary, and that nothing it shows
reads as a trade call. Hand-built candles, no network.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import market_trend as T  # noqa: E402
from test_market_context import TRADE_CALL  # noqa: E402

DAY = 86400
T0 = 1_600_000_000 - 1_600_000_000 % DAY


def series(values):
    return [(datetime.fromtimestamp(T0 + i * DAY, timezone.utc).date().isoformat(), v) for i, v in enumerate(values)]


class TestRule(unittest.TestCase):
    def test_bull_bear_flat(self):
        up = series([100 + i for i in range(300)])
        self.assertEqual(T.states(up)[-1][4], "Bull")
        down = series([400 - i for i in range(300)])
        self.assertEqual(T.states(down)[-1][4], "Bear")
        # a long rise, then a sharp drop under the still-rising average: Flat
        dip = series([100 + i for i in range(280)] + [200] * 5)
        st = T.states(dip)[-1]
        self.assertLess(st[1], st[2])
        self.assertGreater(st[3], 0)
        self.assertEqual(st[4], "Flat")

    def test_needs_a_full_window(self):
        self.assertEqual(T.states(series(range(1, 219))), [])
        self.assertEqual(len(T.states(series(range(1, 221)))), 1)    # 200 for the average + 20 for the slope


class TestSummary(unittest.TestCase):
    def test_since_changes_and_stale(self):
        vals = [100 + i for i in range(300)] + [390 - 3 * i for i in range(80)]
        st = T.states(series(vals))
        last = date.fromisoformat(st[-1][0])
        card = T.summary(st, "us_equity", today=last)
        self.assertEqual(card["state"], "Bear")
        first_bear = next(d for d, *_, s in reversed(st) if s != "Bear")
        self.assertGreater(card["since"], first_bear)
        self.assertGreaterEqual(card["changes_last_year"], 1)
        self.assertFalse(card["stale"])
        self.assertTrue(T.summary(st, "us_equity", today=date.fromordinal(last.toordinal() + 8))["stale"])
        self.assertTrue(T.summary(st, "crypto", today=date.fromordinal(last.toordinal() + 3))["stale"])
        self.assertIsNone(T.summary([], "crypto"))


class TestPanel(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL,"
                          " volume REAL, session TEXT, PRIMARY KEY (pair, ts))")
        for i in range(260):
            self.conn.execute("INSERT INTO candles VALUES ('SPY', ?, 1, 1, 1, ?, 1, 'us_equity')", (T0 + i * DAY, 100 + i))
            for h in range(24):          # crypto: 24 hourly candles a day; the day's last one is its close
                self.conn.execute("INSERT INTO candles VALUES ('BTC-USD', ?, 1, 1, 1, ?, 1, 'crypto')",
                                  (T0 + i * DAY + h * 3600, 500 - i + (h == 23) * 0.5))

    def test_crypto_daily_close_is_the_last_hour(self):
        d = T.daily_closes(self.conn, "BTC-USD", "crypto")
        self.assertEqual(len(d), 260)
        self.assertEqual(d[0][1], 500.5)

    def test_panel_cards_and_wording(self):
        p = T.panel(self.conn)
        m = {x["key"]: x for x in p["markets"]}
        self.assertEqual((m["stocks"]["state"], m["crypto"]["state"], m["gold"]["state"]), ("Bull", "Bear", None))
        self.assertEqual([x["key"] for x in p["markets"]], ["stocks", "crypto", "bonds", "gold", "dollar"])
        texts = [p["rule"], p["note"]] + [str(v) for x in p["markets"] for v in x.values() if isinstance(v, str)]
        for t in texts:
            self.assertIsNone(TRADE_CALL.search(t), t)


if __name__ == "__main__":
    unittest.main()
