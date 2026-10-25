"""
Tests for pinned IS/OOS windows (P2.1). In-memory SQLite, synthetic candles.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import optimizer  # noqa: E402
import windows as W  # noqa: E402

T0 = 1_725_242_400          # 2024-09-02 02:00 UTC, first real candle
HOUR = 3600


def make_db(n_bars, pairs=("BTC-USD",)):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL,"
                 " low REAL, close REAL, volume REAL, PRIMARY KEY (pair, ts))")
    for p in pairs:
        add_bars(conn, p, 0, n_bars)
    optimizer.ensure_tables(conn)
    return conn


def add_bars(conn, pair, start, stop):
    conn.executemany("INSERT INTO candles VALUES (?, ?, 1, 1, 1, 1, 1)",
                     [(pair, T0 + i * HOUR) for i in range(start, stop)])


def insert_candidate(conn, run_at, pinned=None):
    cols = "run_at, pair, tf, params_json, passed"
    vals = [run_at, "BTC-USD", "1h", "{}", 0]
    if pinned:
        cols += ", is_start, split_ts, oos_end, windows_backfilled"
        vals += list(pinned) + [0]
    cur = conn.execute(f"INSERT INTO candidates ({cols}) VALUES"
                       f" ({', '.join('?' * len(vals))})", vals)
    return cur.lastrowid


class DBTestCase(unittest.TestCase):
    def db(self, *args):
        conn = make_db(*args)
        self.addCleanup(conn.close)
        return conn


class TestCompute(DBTestCase):
    def test_split_rule_matches_optimizer(self):
        conn = self.db(1000)
        s, sp, e = W.compute(conn, "BTC-USD", HOUR)
        self.assertEqual(W._ts(s), T0 + 300 * HOUR)
        self.assertEqual(W._ts(sp), T0 + int(999 * HOUR * 0.70))
        self.assertEqual(W._ts(e), T0 + 999 * HOUR)

    def test_as_of_ignores_later_candles(self):
        conn = self.db(1000)
        cut = T0 + 600 * HOUR
        self.assertEqual(W._ts(W.compute(conn, "BTC-USD", HOUR, cut)[2]), cut)

    def test_no_candles(self):
        self.assertIsNone(W.compute(self.db(10), "ETH-USD", HOUR))


class TestPinning(DBTestCase):
    def test_round_trip_survives_new_candles(self):
        conn = self.db(1000)
        pinned = W.compute(conn, "BTC-USD", HOUR)
        cid = insert_candidate(conn, "2024-10-13 18:00:00", pinned)
        add_bars(conn, "BTC-USD", 1000, 2000)     # collector keeps running
        self.assertEqual(W.for_candidate(conn, cid), pinned)
        self.assertNotEqual(W.compute(conn, "BTC-USD", HOUR), pinned)  # the drift P2.1 stops

    def test_missing_windows_refuse_rather_than_recompute(self):
        conn = self.db(1000)
        cid = insert_candidate(conn, "2024-10-13 18:00:00")
        with self.assertRaises(SystemExit):
            W.for_candidate(conn, cid)

    def test_backfill_uses_run_at_and_marks_rows(self):
        conn = self.db(2000)
        run_at_ts = T0 + 1000 * HOUR + 1234       # mid-bar, 20 min after a bar opened
        run_at = W._iso(run_at_ts).replace("T", " ")
        old = insert_candidate(conn, run_at)
        keep = W.compute(conn, "BTC-USD", HOUR)
        new = insert_candidate(conn, "2026-01-01 00:00:00", keep)
        n, todo = W.backfill(conn)
        self.assertEqual((n, todo), (1, 1))
        self.assertEqual(W.for_candidate(conn, old),
                         W.compute(conn, "BTC-USD", HOUR, T0 + 1000 * HOUR))
        self.assertEqual(W.for_candidate(conn, new), keep)   # pinned rows untouched
        flags = dict(conn.execute("SELECT id, windows_backfilled FROM candidates"))
        self.assertEqual(flags, {old: 1, new: 0})

    def test_ensure_columns_idempotent(self):
        conn = self.db(10)
        W.ensure_columns(conn)
        W.ensure_columns(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(candidates)")]
        for name, _ in W.COLUMNS:
            self.assertEqual(cols.count(name), 1)


if __name__ == "__main__":
    unittest.main()
