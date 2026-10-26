"""
Tests for the collector's candle backfill (P2.4.1). In-memory SQLite and a
fake Coinbase API — no network.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import main  # noqa: E402

HOUR = 3600
NOW = int(datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).timestamp())


class FrozenDatetime(datetime):
    """main.datetime with now() pinned to NOW, so a test run that crosses an
    hour boundary can't disagree with the fake API."""

    @classmethod
    def now(cls, tz=None):
        return datetime.fromtimestamp(NOW + 1234, tz)


class FakeCoinbase:
    """Hourly bars from listed_ts to NOW, newest first like the real API.
    Records every (start, end) request."""

    def __init__(self, listed_ts):
        self.listed_ts = listed_ts
        self.calls = []

    def __call__(self, pair, start, end):
        s, e = int(start.timestamp()), int(end.timestamp())
        self.calls.append((s, e))
        assert e - s <= 300 * HOUR, "chunk larger than 300 bars"
        first = max(s, self.listed_ts)
        return [[t, 1.0, 2.0, 1.5, 1.6, 10.0]
                for t in range(min(e, NOW), first - 1, -HOUR) if t >= first]


class TestUpdateCandles(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE candles (pair TEXT NOT NULL, ts INTEGER NOT NULL,"
                          " open REAL, high REAL, low REAL, close REAL, volume REAL,"
                          " PRIMARY KEY (pair, ts))")
        self.conn.execute("CREATE TABLE poll_errors (ts INTEGER, source TEXT, error TEXT)")
        main._history_exhausted.clear()
        for p in (mock.patch("main.time.sleep"), mock.patch("main.log"),
                  mock.patch("main.datetime", FrozenDatetime)):
            p.start()
            self.addCleanup(p.stop)

    def seed(self, first_ts, last_ts):
        main.store_candles(self.conn, "BTC-USD", [[t, 1, 2, 1.5, 1.6, 10]
                                                  for t in range(first_ts, last_ts + 1, HOUR)])

    def run_update(self, fake, days):
        with mock.patch("main.fetch_coinbase_candles", fake), \
                mock.patch("main.BACKFILL_DAYS", days):
            return main.update_candles(self.conn, "BTC-USD")

    def span(self):
        lo, hi, n = self.conn.execute(
            "SELECT MIN(ts), MAX(ts), COUNT(*) FROM candles WHERE pair='BTC-USD'").fetchone()
        return lo, hi, n

    def test_backfills_backwards_to_target_then_tops_up(self):
        self.seed(NOW - 10 * 86400, NOW - 5 * HOUR)
        fake = FakeCoinbase(listed_ts=NOW - 400 * 86400)
        self.run_update(fake, days=60)
        lo, hi, n = self.span()
        self.assertEqual(lo, NOW - 60 * 86400)
        self.assertEqual(hi, NOW)
        self.assertEqual(n, (hi - lo) // HOUR + 1)            # no gaps
        backward = [c for c in fake.calls if c[1] <= NOW - 10 * 86400]
        self.assertEqual(len(backward), -(-50 * 24 // 300))   # ceil(1200 / 300) chunks
        self.assertLess(fake.calls.index(backward[-1]),        # backward before forward
                        fake.calls.index((NOW - 5 * HOUR, NOW)))

    def test_stops_at_listing_date_and_does_not_retry(self):
        listed = NOW - 30 * 86400
        self.seed(NOW - 10 * 86400, NOW)
        fake = FakeCoinbase(listed_ts=listed)
        self.run_update(fake, days=2000)
        self.assertEqual(self.span()[0], listed)
        self.assertIn("BTC-USD", main._history_exhausted)
        fake.calls.clear()
        self.run_update(fake, days=2000)                       # next hourly run
        self.assertTrue(all(s >= NOW - HOUR for s, _ in fake.calls), fake.calls)

    def test_no_backward_fetch_when_history_already_covers_target(self):
        self.seed(NOW - 40 * 86400, NOW - 2 * HOUR)
        fake = FakeCoinbase(listed_ts=NOW - 400 * 86400)
        self.run_update(fake, days=30)
        self.assertEqual(fake.calls, [(NOW - 2 * HOUR, NOW)])  # forward top-up only
        self.assertEqual(self.span()[0], NOW - 40 * 86400)

    def test_empty_table_unchanged_behaviour(self):
        fake = FakeCoinbase(listed_ts=NOW - 400 * 86400)
        self.run_update(fake, days=20)
        lo, hi, n = self.span()
        self.assertEqual((lo, hi), (NOW - 20 * 86400, NOW))
        self.assertTrue(all(s >= NOW - 20 * 86400 for s, _ in fake.calls))

    def test_backward_error_still_tops_up_and_retries_later(self):
        self.seed(NOW - 10 * 86400, NOW - 3 * HOUR)
        ok = FakeCoinbase(listed_ts=NOW - 400 * 86400)

        def flaky(pair, start, end):
            if end.timestamp() <= NOW - 10 * 86400:
                raise OSError("HTTP Error 503")
            return ok(pair, start, end)

        self.run_update(flaky, days=60)
        lo, hi, _ = self.span()
        self.assertEqual((lo, hi), (NOW - 10 * 86400, NOW))
        self.assertNotIn("BTC-USD", main._history_exhausted)
        errs = self.conn.execute("SELECT source FROM poll_errors").fetchall()
        self.assertEqual(errs, [("coinbase-candles-back-BTC-USD",)])


if __name__ == "__main__":
    unittest.main()
