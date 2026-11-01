"""
P9.1: the equities data layer. No network: fetches are stubbed.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import engine  # noqa: E402
import equities as EQ  # noqa: E402

D0 = 1704067200    # Monday 2024-01-01 00:00 UTC
DAY = 86400


def crypto_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE candles (pair TEXT NOT NULL, ts INTEGER NOT NULL, open REAL,
                    high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (pair, ts))""")
    for pair in ("BTC-USD", "ETH-USD"):
        conn.execute("INSERT INTO candles VALUES (?, ?, 1, 2, 0.5, 1.5, 10)", (pair, D0))
    conn.commit()
    return conn


def bars(n=250):
    out = []
    i = 0
    while len(out) < n:
        ts = D0 + i * DAY
        if (i % 7) < 5:
            out.append((ts, 100.0, 101.0, 99.0, 100.5, 1000.0))
        i += 1
    return out


class TestSchema(unittest.TestCase):
    def test_crypto_rows_read_crypto_and_are_unchanged(self):
        conn = crypto_db()
        before = conn.execute("SELECT * FROM candles ORDER BY pair").fetchall()
        EQ.ensure_tables(conn)
        EQ.ensure_tables(conn)      # idempotent
        rows = conn.execute("SELECT * FROM candles ORDER BY pair").fetchall()
        self.assertEqual([r[:-1] for r in rows], before)
        self.assertEqual({r[-1] for r in rows}, {"crypto"})

    def test_seed_is_the_ten_etfs(self):
        conn = crypto_db()
        EQ.ensure_tables(conn)
        EQ.seed_universe(conn)
        EQ.seed_universe(conn)
        got = conn.execute("SELECT symbol, asset_class, tick_size, session FROM instruments").fetchall()
        self.assertEqual({r[0] for r in got},
                         set("SPY QQQ IWM EFA EEM TLT IEF GLD DBC UUP".split()))
        self.assertEqual({r[1:] for r in got}, {("etf", 0.01, "us_equity")})


class TestSessionPairs(unittest.TestCase):
    def test_crypto_never_sees_etfs_and_the_reverse(self):
        conn = crypto_db()
        self.assertEqual(engine.session_pairs(conn, "BTC-USD"), ["BTC-USD", "ETH-USD"])  # old schema
        EQ.ensure_tables(conn)
        EQ.store_bars(conn, "SPY", bars(3))
        EQ.store_bars(conn, "TLT", bars(3))
        self.assertEqual(engine.session_pairs(conn, "BTC-USD"), ["BTC-USD", "ETH-USD"])
        self.assertEqual(engine.session_pairs(conn, "SPY"), ["SPY", "TLT"])


class TestParse(unittest.TestCase):
    def test_trading_date_is_independent_of_dst(self):
        for t in ("2024-01-02T05:00:00Z", "2024-07-02T04:00:00Z"):
            ts = EQ._parse({"t": t, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1})[0]
            self.assertEqual(ts % DAY, 0)
            self.assertEqual(EQ._date(ts), t[:10])


class TestCheckBars(unittest.TestCase):
    def test_good_series_passes(self):
        EQ.check_bars("SPY", bars(10))

    def test_rejects_empty_weekend_duplicate_and_bad_ohlc(self):
        good = bars(5)
        sat = D0 + 5 * DAY
        for bad in ([], good + [(sat, 1, 2, 0.5, 1, 1)], good + [good[-1]],
                    [(D0, 100.0, 99.0, 98.0, 100.5, 1.0)]):
            with self.assertRaises(ValueError):
                EQ.check_bars("SPY", bad)


class TestStoreBars(unittest.TestCase):
    def test_refuses_a_symbol_that_is_crypto(self):
        conn = crypto_db()
        EQ.ensure_tables(conn)
        with self.assertRaises(ValueError):
            EQ.store_bars(conn, "BTC-USD", bars(1))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM candles").fetchone()[0], 2)

    def test_commits_in_batches(self):
        conn = crypto_db()
        EQ.ensure_tables(conn)
        commits = []
        conn.set_trace_callback(lambda s: commits.append(s) if s.startswith("COMMIT") else None)
        EQ.store_bars(conn, "SPY", bars(250))
        self.assertEqual(len(commits), 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM candles WHERE pair='SPY'").fetchone()[0], 250)


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.conn = crypto_db()
        EQ.ensure_tables(self.conn)
        EQ.seed_universe(self.conn)
        self.fetched = []

    def fetch(self, sym, end, headers):
        self.fetched.append(sym)
        if sym == getattr(self, "die_on", None):
            raise OSError("network")
        return bars(10)

    def run_it(self, **kw):
        return EQ.backfill(self.conn, {}, today=date(2026, 11, 1), fetch=self.fetch,
                           exchange=lambda s, h: "ARCA", **kw)

    def test_records_depth_and_resumes_after_a_failure(self):
        self.die_on = "IEF"
        with self.assertRaises(OSError):
            self.run_it()
        self.die_on = None
        first_run = list(self.fetched)
        self.fetched.clear()
        self.run_it()
        self.assertEqual(self.fetched[0], "IEF")
        self.assertFalse(set(first_run[:-1]) & set(self.fetched))
        row = self.conn.execute("SELECT data_start, n_bars, exchange, adjustment, fetched_through"
                                " FROM instruments WHERE symbol='SPY'").fetchone()
        self.assertEqual(row, ("2024-01-01", 10, "ARCA", "all", "2026-10-31"))

    def test_current_symbols_are_skipped_unless_forced(self):
        self.run_it()
        self.fetched.clear()
        self.assertEqual(self.run_it(), [])
        self.assertEqual(len(self.run_it(force=True)), 10)

    def test_a_bad_series_writes_nothing(self):
        self.fetch = lambda s, e, h: []
        with self.assertRaises(ValueError):
            self.run_it()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM candles WHERE session='us_equity'")
                         .fetchone()[0], 0)


class TestCredentials(unittest.TestCase):
    def test_reads_the_two_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "alpaca.env")
            with open(p, "w") as f:
                f.write("# paper\nAPCA_API_KEY_ID=abc\nAPCA_API_SECRET_KEY='def'\n")
            self.assertEqual(EQ.load_credentials(p),
                             {"APCA-API-KEY-ID": "abc", "APCA-API-SECRET-KEY": "def"})

    def test_missing_file_or_key_stops(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(SystemExit):
                EQ.load_credentials(os.path.join(d, "alpaca.env"))
            p = os.path.join(d, "alpaca.env")
            with open(p, "w") as f:
                f.write("APCA_API_KEY_ID=abc\n")
            with self.assertRaises(SystemExit):
                EQ.load_credentials(p)


if __name__ == "__main__":
    unittest.main()
