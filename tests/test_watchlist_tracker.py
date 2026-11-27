"""
Tests for the P9.4 watchlist (watchlist.py add/remove/list, web.py POST/DELETE
/api/watchlist): validation against instruments, history kept, the token, and
the authorizer that fences the only writable connection to the watchlist table.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fast_tmp  # noqa: E402,F401
import watchlist as WL  # noqa: E402
import web  # noqa: E402

TOKEN = "w" * 24


def make_db(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE instruments (symbol TEXT PRIMARY KEY, asset_class TEXT, session TEXT)")
    conn.executemany("INSERT INTO instruments VALUES (?, ?, 'us_equity')", [("AAPL", "equity"), ("SPY", "etf")])
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, close REAL)")
    WL.ensure_watch_table(conn)
    conn.commit()
    return conn


class TestStore(unittest.TestCase):
    def setUp(self):
        self.conn = make_db(":memory:")

    def test_add_validates_and_dedupes(self):
        rid, created = WL.add(self.conn, "aapl", notes="look at margins", today="2026-11-27")
        self.assertTrue(created)
        self.assertEqual(WL.add(self.conn, "AAPL")[0], rid)                 # already active
        for bad in ("SPY", "NOPE", "", None, "X" * 11):                      # an ETF is not an equity
            with self.assertRaises(ValueError):
                WL.add(self.conn, bad)
        with self.assertRaises(ValueError):
            WL.add(self.conn, "AAPL", "screenx", notes="x" * 501)

    def test_remove_keeps_history_and_spares_screens(self):
        WL.add(self.conn, "AAPL", today="2026-11-27")
        WL.add(self.conn, "AAPL", "momentum", {"score": 2.1}, today="2026-11-27")
        self.assertEqual(WL.remove(self.conn, "AAPL", today="2026-11-28"), 1)
        rows = {(r["added_by"], r["status"]) for r in WL.rows(self.conn, include_inactive=True)}
        self.assertEqual(rows, {("user", "removed"), ("momentum", "active")})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0], 2)


class TestAuthorizer(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = os.path.join(self.dir, "forge.db")
        make_db(self.db).close()

    def test_only_watchlist_writes(self):
        conn = web.connect_watchlist(self.db)
        self.addCleanup(conn.close)
        WL.add(conn, "AAPL")
        for sql in ("INSERT INTO candles VALUES ('X', 1, 1)", "UPDATE instruments SET session='x'",
                    "DELETE FROM watchlist", "DROP TABLE candles", "CREATE TABLE t (x)",
                    "SELECT * FROM candles", "PRAGMA journal_mode=WAL", "ATTACH ':memory:' AS other"):
            with self.assertRaises(sqlite3.DatabaseError, msg=sql):
                conn.execute(sql)
        self.assertEqual(len(WL.rows(conn)), 1)


class TestHttp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.db = os.path.join(cls.dir, "forge.db")
        make_db(cls.db).close()
        cls.srv = web.make_server("127.0.0.1", 0, cls.db, TOKEN)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def call(self, method, path, body=None, token=TOKEN, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null")

    def test_add_list_remove(self):
        self.assertEqual(self.call("POST", "/api/watchlist", {"ticker": "AAPL"}, token=None)[0], 401)
        self.assertEqual(self.call("POST", "/api/watchlist", {"ticker": "AAPL"}, token="x" * 24)[0], 401)
        st, body = self.call("POST", "/api/watchlist", {"ticker": "AAPL", "notes": "margins"})
        self.assertEqual((st, body["created"]), (200, True))
        self.assertEqual(self.call("POST", "/api/watchlist", {"ticker": "NOPE"})[0], 400)
        self.assertEqual(self.call("POST", "/api/watchlist", raw=b"not json")[0], 400)
        self.assertEqual(self.call("POST", "/api/watchlist", raw=b"x" * 5000)[0], 413)
        st, rows = self.call("GET", "/api/watchlist")
        self.assertEqual([r["ticker"] for r in rows["rows"]], ["AAPL"])
        self.assertEqual(self.call("DELETE", "/api/watchlist/AAPL", token=None)[0], 401)
        self.assertEqual(self.call("DELETE", "/api/watchlist/AAPL"), (200, {"removed": 1}))
        st, rows = self.call("GET", "/api/watchlist")
        self.assertEqual(rows["rows"][0]["status"], "removed")

    def test_everything_else_stays_read_only(self):
        self.assertEqual(self.call("POST", "/api/candidates", {})[0], 405)
        self.assertEqual(self.call("DELETE", "/api/health")[0], 405)
        self.assertEqual(self.call("PUT", "/api/watchlist", {})[0], 405)


if __name__ == "__main__":
    unittest.main()
