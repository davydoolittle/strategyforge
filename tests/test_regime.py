"""
Tests for P3.4: features.RegimeTagger (daily trend_up / trend_down / chop at
signal time), the regime column in trade_features + its backfill, and
adversary A9 (regime_split).
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import random
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import adversary as A  # noqa: E402
import features  # noqa: E402

H = 3600
D = 86400
DAY0 = 18_000 * D          # 2019-04-14, midnight UTC


def candles_db(daily_path):
    """Hourly candles whose daily closes follow daily_path; each day moves
    smoothly from the previous close, with a small intraday range."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
                 " close REAL, volume REAL, PRIMARY KEY (pair, ts))")
    prev = daily_path[0]
    for d, close in enumerate(daily_path):
        for h in range(24):
            o = prev + (close - prev) * h / 24
            c = prev + (close - prev) * (h + 1) / 24
            conn.execute("INSERT INTO candles VALUES ('BTC-USD', ?, ?, ?, ?, ?, 1)",
                         (DAY0 + d * D + h * H, o, max(o, c) * 1.0005, min(o, c) * 0.9995, c))
        prev = close
    return conn


def sig(day, hour=12):
    return DAY0 + day * D + hour * H


class TestRegimeTagger(unittest.TestCase):
    def test_up_down_chop(self):
        rng = random.Random(3)
        path = [100 * 1.01 ** d for d in range(120)]                   # steady rise
        path += [path[-1] * 0.99 ** d for d in range(1, 121)]          # steady fall
        path += [path[-1] * (1 + 0.02 * (-1) ** d + rng.gauss(0, 0.002)) for d in range(150)]  # chop
        conn = candles_db(path)
        self.addCleanup(conn.close)
        rt = features.RegimeTagger(conn, "BTC-USD")
        self.assertIsNone(rt.at(sig(10)))                               # EMA warmup
        self.assertEqual(rt.at(sig(110)), "trend_up")
        self.assertEqual(rt.at(sig(230)), "trend_down")
        self.assertEqual(rt.at(sig(380)), "chop")

    def test_uses_previous_completed_day_only(self):
        path = [100 * 1.01 ** d for d in range(100)]
        conn = candles_db(path)
        self.addCleanup(conn.close)
        before = features.RegimeTagger(conn, "BTC-USD").at(sig(99, 1))
        # crash the signal's own day: the tag at 01:00 must not see it
        conn.execute("UPDATE candles SET open=1, high=1, low=0.5, close=0.5 WHERE ts >= ?",
                     (DAY0 + 99 * D + 2 * H,))
        self.assertEqual(features.RegimeTagger(conn, "BTC-USD").at(sig(99, 1)), before)
        self.assertEqual(before, "trend_up")

    def test_missing_previous_day(self):
        conn = candles_db([100.0] * 80)
        self.addCleanup(conn.close)
        self.assertIsNone(features.RegimeTagger(conn, "BTC-USD").at(sig(200)))


class TestRegimeColumn(unittest.TestCase):
    def test_column_added_and_backfilled(self):
        conn = candles_db([100 * 1.01 ** d for d in range(100)])
        self.addCleanup(conn.close)
        conn.execute("""CREATE TABLE trade_features (id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER, pair TEXT, tf_seconds INTEGER, signal_ts INTEGER,
            entry_ts INTEGER, direction TEXT, pnl REAL, reason TEXT, features_json TEXT)""")
        for d in (5, 90, 95):                                           # day 5: warmup
            conn.execute("INSERT INTO trade_features (candidate_id, pair, tf_seconds, signal_ts,"
                         " pnl, features_json) VALUES (1, 'BTC-USD', 3600, ?, 1, '{}')", (sig(d),))
        n = features.backfill_regime(conn, progress=False)
        self.assertEqual(n, 2)
        got = conn.execute("SELECT signal_ts, regime FROM trade_features ORDER BY id").fetchall()
        self.assertEqual([r for _, r in got], [None, "trend_up", "trend_up"])
        self.assertEqual(features.backfill_regime(conn, progress=False), 0)  # warmup row stays


def t(pnl, reg):
    return {"pnl": pnl, "reg": reg}


class TestA9(unittest.TestCase):
    def split(self, trades):
        return A.regime_split(trades, lambda x: x["reg"])

    def test_profit_in_two_regimes_passes(self):
        r = self.split([t(100, "trend_up"), t(50, "chop"), t(-20, "trend_down")])
        self.assertEqual(r["best_regime"], "trend_up")
        self.assertAlmostEqual(r["net_outside_best"], 30)
        self.assertTrue(r["passed"])

    def test_all_profit_in_one_regime_fails(self):
        r = self.split([t(100, "trend_up"), t(30, "chop"), t(-40, "trend_down")])
        self.assertAlmostEqual(r["net_outside_best"], -10)
        self.assertFalse(r["passed"])

    def test_single_regime_fails(self):
        self.assertFalse(self.split([t(100, "trend_up"), t(50, "trend_up")])["passed"])

    def test_warmup_trades_reported_not_judged(self):
        r = self.split([t(100, "trend_up"), t(500, None), t(-5, "chop")])
        self.assertEqual(r["regimes"]["n/a"]["n_trades"], 1)
        self.assertFalse(r["passed"])
        self.assertIsNone(r["regimes"]["trend_up"]["pf"])               # no losses: pf None

    def test_pf_per_regime(self):
        r = self.split([t(30, "chop"), t(-10, "chop"), t(5, "trend_up")])
        self.assertAlmostEqual(r["regimes"]["chop"]["pf"], 3.0)

    def test_no_trades(self):
        self.assertFalse(self.split([])["passed"])


if __name__ == "__main__":
    unittest.main()
