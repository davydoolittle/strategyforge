"""
The Edgar feed. No network: Edgar and ntfy are stubbed.
"""
import os
import sqlite3
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import insider_feed as F  # noqa: E402

TODAY = date(2026, 11, 2)
NOW_MS = 1_793_600_000_000


def txn(i, ticker="XYZ", code="P", value=500_000.0, d="2026-10-30"):
    return {"id": i, "accession": f"a{i}", "ticker": ticker, "owner_name": "Pat Doe", "owner_title": "CEO",
            "txn_date": d, "code": code, "value": value, "direction": "buy", "filing_date": d}


def cluster(key="XYZ|a~b", ticker="XYZ", end="2026-10-30"):
    return {"cluster_key": key, "ticker": ticker, "window_start": "2026-10-20", "window_end": end,
            "insider_count": 3, "buyers": "a, b, c", "buy_count": 3, "total_value": 2_100_000.0,
            "created_at": 1}


def edgar(txns=(), clusters=(), last="recent"):
    # "recent" = polled a minute before the real clock, because run() checks staleness against now
    def get(url):
        lp = (datetime.now(timezone.utc).timestamp() - 60) * 1000 if last == "recent" else last
        return {"txns": [t for t in txns if t["id"] > int(url.split("after=")[1].split("&")[0])],
                "clusters": list(clusters), "last_poll_ok": lp}
    return get


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        F.ensure_tables(self.conn)


class TestPull(Base):
    def test_a_rerun_adds_nothing(self):
        for _ in range(2):
            F.pull(self.conn, "http://x", edgar([txn(1), txn(2)], [cluster()]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM insider_txns").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM insider_clusters").fetchone()[0], 1)

    def test_pages_until_a_short_page(self):
        all_t = [txn(i) for i in range(1, 6)]
        old, F.PAGE = F.PAGE, 2

        def get(url):
            a = int(url.split("after=")[1].split("&")[0])
            return {"txns": [t for t in all_t if t["id"] > a][:2], "clusters": [], "last_poll_ok": NOW_MS}
        try:
            F.pull(self.conn, "http://x", get)
        finally:
            F.PAGE = old
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM insider_txns").fetchone()[0], 5)


class TestDigest(Base):
    def load(self, txns=(), clusters=()):
        F.pull(self.conn, "http://x", edgar(txns, clusters))

    def test_only_large_recent_open_market_buys(self):
        self.load([txn(1), txn(2, code="S"), txn(3, code="A"), txn(4, value=5000.0),
                   txn(5, d="2026-09-01")])
        lines = F.digest(self.conn, TODAY)
        self.assertEqual(len(lines), 1)
        self.assertIn("XYZ: Pat Doe (CEO) bought $500K on 2026-10-30", lines[0])

    def test_old_clusters_are_marked_and_never_sent(self):
        self.load(clusters=[cluster(end="2026-09-01")])
        self.assertEqual(F.digest(self.conn, TODAY), [])
        self.assertEqual(self.conn.execute("SELECT notified FROM insider_clusters").fetchone()[0], 1)

    def test_crypto_linked_names_are_labelled(self):
        self.load([txn(1, ticker="COIN")], [cluster(ticker="MARA")])
        lines = F.digest(self.conn, TODAY)
        self.assertIn("CLUSTER MARA (crypto-linked): 3 insiders bought $2.1M", lines[0])
        self.assertIn("COIN (crypto-linked)", lines[1])

    def test_lines_are_capped(self):
        self.load([txn(i) for i in range(1, 30)])
        lines = F.digest(self.conn, TODAY)
        self.assertEqual(len(lines), F.MAX_LINES + 1)
        self.assertIn("17 more", lines[-1])


class TestRun(Base):
    def run_it(self, get, notify=True):
        self.sent = []
        return F.run(self.conn, notify, "http://x", get,
                     lambda t, title, body: self.sent.append((t, title, body)), "topic")

    def test_a_message_says_untested_and_is_not_repeated(self):
        self.run_it(edgar([txn(1, d=date.today().isoformat())]))
        self.assertEqual(self.sent[0][1], "Insider activity")
        self.assertIn(F.TAG, self.sent[0][2])
        self.assertNotIn("likely", self.sent[0][2].lower())
        self.run_it(edgar([txn(1, d=date.today().isoformat())]))
        self.assertEqual(self.sent, [])

    def test_no_notify_sends_and_marks_nothing(self):
        self.run_it(edgar([txn(1, d=date.today().isoformat())]), notify=False)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.conn.execute("SELECT notified FROM insider_txns").fetchone()[0], 0)

    def test_a_dead_edgar_says_so(self):
        def down(url):
            raise OSError("refused")
        self.run_it(down)
        self.assertEqual(self.sent[0][1], "Edgar is down")

    def test_a_stale_edgar_says_so_even_with_nothing_new(self):
        self.run_it(edgar(last=1000))
        self.assertEqual(self.sent[0][1], "Edgar problem")

    def test_quiet_days_send_nothing(self):
        import time
        self.run_it(edgar(last=time.time() * 1000))
        self.assertEqual(self.sent, [])


class TestHealth(unittest.TestCase):
    def test_health(self):
        self.assertIsNone(F.health_problem({"last_poll_ok": NOW_MS}, NOW_MS))
        self.assertIn("7 hours", F.health_problem({"last_poll_ok": NOW_MS - 7 * 3_600_000}, NOW_MS))
        self.assertIn("no successful", F.health_problem({"last_poll_ok": None}, NOW_MS))


if __name__ == "__main__":
    unittest.main()
