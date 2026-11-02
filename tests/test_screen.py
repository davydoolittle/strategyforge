"""
Tests for the momentum screen (P9.1b): known-answer returns and volatility,
ranking, the extreme flag, short history, staleness, read-only JSON output.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fast_tmp  # noqa: E402,F401  (temp files in RAM, see fast_tmp.py)

import equities as EQ  # noqa: E402
import screen as SC  # noqa: E402

DAY = 86400
T0 = 1_600_000_000 - 1_600_000_000 % DAY


def make_db(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE candles (pair TEXT NOT NULL, ts INTEGER NOT NULL, open REAL,
                    high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY (pair, ts))""")
    EQ.ensure_tables(conn)
    return conn


def add(conn, symbol, closes, end_day=400, session="us_equity"):
    conn.execute("INSERT OR IGNORE INTO instruments (symbol, asset_class, session) VALUES (?, 'etf', ?)",
                 (symbol, session))
    start = end_day - len(closes) + 1
    conn.executemany("INSERT INTO candles (pair, ts, open, high, low, close, volume, session)"
                     " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                     [(symbol, T0 + (start + i) * DAY, c, c, c, c, session) for i, c in enumerate(closes)])
    conn.commit()


def growth(n, daily):
    """n closes growing by a constant daily factor: zero volatility."""
    return [100 * daily ** i for i in range(n)]


def zigzag(n, daily, amp):
    """Constant drift plus alternating +-amp log moves: known volatility."""
    out, lp = [], math.log(100)
    for i in range(n):
        out.append(math.exp(lp))
        lp += math.log(daily) + (amp if i % 2 == 0 else -amp)
    return out


class TestMetrics(unittest.TestCase):
    def test_returns_are_exact(self):
        m = SC.metrics([(i, c) for i, c in enumerate(growth(300, 1.001))])
        for k, n in SC.HORIZONS.items():
            self.assertAlmostEqual(m[f"ret_{k}"], 1.001 ** n - 1, places=12)
        self.assertAlmostEqual(m["vol_12m"], 0.0, places=12)
        self.assertIsNone(m["vol_adj_12m"])                 # no division by zero

    def test_vol_and_vol_adjusted(self):
        px = zigzag(253, 1.0, 0.01)
        m = SC.metrics(list(enumerate(px)))
        rets = [math.log(b / a) for a, b in zip(px, px[1:])]
        mean = sum(rets) / len(rets)
        want = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252)
        self.assertAlmostEqual(m["vol_12m"], want, places=12)
        self.assertAlmostEqual(m["vol_12m"], 0.01 * math.sqrt(252), places=3)
        self.assertAlmostEqual(m["vol_adj_12m"], m["ret_12m"] / m["vol_12m"], places=12)

    def test_needs_253_closes(self):
        self.assertIsNone(SC.metrics(list(enumerate(growth(252, 1.001)))))
        self.assertIsNotNone(SC.metrics(list(enumerate(growth(253, 1.001)))))

    def test_n_extreme(self):
        self.assertEqual([SC.n_extreme(n) for n in (1, 2, 10, 14, 15, 20)], [0, 1, 1, 1, 2, 2])


class TestCompute(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        # ten instruments, same volatility, 12-month drift ordered A (best) .. J (worst)
        for i, sym in enumerate("ABCDEFGHIJ"):
            add(self.conn, sym, zigzag(300, 1.0010 - 0.0002 * i, 0.01))

    def test_rank_by_vol_adjusted_12m(self):
        sc = SC.compute(self.conn)
        self.assertEqual([r["symbol"] for r in sc["rows"]], list("ABCDEFGHIJ"))
        self.assertEqual([r["rank"] for r in sc["rows"]], list(range(1, 11)))
        vals = [r["vol_adj_12m"] for r in sc["rows"]]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertEqual(sc["note"], "ranking, not a validated signal")

    def test_vol_adjusted_beats_raw_return(self):
        # Z: higher 12-month return than A but four times the volatility -> ranks below A
        add(self.conn, "Z", zigzag(300, 1.0012, 0.04))
        rows = SC.compute(self.conn)["rows"]
        z = next(r for r in rows if r["symbol"] == "Z")
        a = next(r for r in rows if r["symbol"] == "A")
        self.assertGreater(z["ret_12m"], a["ret_12m"])
        self.assertGreater(z["rank"], a["rank"])

    def test_extreme_flags_top_and_bottom_1m(self):
        # last 21 days: K jumps, L slumps; both otherwise middling
        base = zigzag(300, 1.0003, 0.01)
        add(self.conn, "K", base[:-21] + [c * 1.30 for c in base[-21:]])
        add(self.conn, "L", base[:-21] + [c * 0.70 for c in base[-21:]])
        rows = {r["symbol"]: r for r in SC.compute(self.conn)["rows"]}
        flagged = {s: r["extreme"] for s, r in rows.items() if r["extreme"]}
        self.assertEqual(flagged, {"K": "top", "L": "bottom"})          # 12 symbols -> 1 each side
        self.assertEqual(rows["K"]["flags"], [SC.EXTREME_LABEL])
        self.assertEqual(rows["A"]["flags"], [])

    def test_short_history_and_other_sessions_are_left_out(self):
        add(self.conn, "NEW", growth(100, 1.001))
        add(self.conn, "BTC-USD", growth(300, 1.001), session="crypto")
        sc = SC.compute(self.conn)
        self.assertNotIn("NEW", [r["symbol"] for r in sc["rows"]])
        self.assertNotIn("BTC-USD", [r["symbol"] for r in sc["rows"]] + [s["symbol"] for s in sc["skipped"]])
        self.assertEqual(sc["skipped"], [{"symbol": "NEW", "bars": 100, "why": "fewer than 253 daily bars"}])
        self.assertIn("NEW", SC.render(sc))

    def test_stale_rows_are_marked(self):
        add(self.conn, "OLD", zigzag(300, 1.0003, 0.01), end_day=390)
        sc = SC.compute(self.conn)
        old = next(r for r in sc["rows"] if r["symbol"] == "OLD")
        self.assertTrue(old["stale"])
        self.assertIn("stale: last bar", old["flags"][-1])
        self.assertFalse(any(r["stale"] for r in sc["rows"] if r["symbol"] != "OLD"))

    def test_render_says_not_a_signal(self):
        text = SC.render(SC.compute(self.conn))
        self.assertIn("RANKING, NOT A VALIDATED SIGNAL", text)
        self.assertIn(" 1 A ", text)

    def test_empty_universe(self):
        sc = SC.compute(make_db())
        self.assertEqual((sc["rows"], sc["skipped"], sc["as_of"]), ([], [], None))
        self.assertIn("no us_equity instruments", SC.render(sc))


class TestCli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.db = os.path.join(self.dir, "forge.db")
        conn = make_db(self.db)
        for i, sym in enumerate("ABC"):
            add(conn, sym, zigzag(300, 1.001 - 0.0003 * i, 0.01))
        conn.close()

    def test_writes_json_and_never_the_db(self):
        before = os.path.getmtime(self.db), os.path.getsize(self.db)
        with unittest.mock.patch.object(SC.main, "DB_PATH", self.db), \
                unittest.mock.patch("sys.stdout"):
            SC.main_cli([])
        with open(SC.default_out(self.db)) as f:
            sc = json.load(f)
        self.assertEqual([r["symbol"] for r in sc["rows"]], ["A", "B", "C"])
        self.assertEqual(sc["note"], "ranking, not a validated signal")
        self.assertFalse(os.path.exists(SC.default_out(self.db) + ".tmp"))
        self.assertEqual((os.path.getmtime(self.db), os.path.getsize(self.db)), before)

    def test_read_only_connection(self):
        with unittest.mock.patch.object(SC.main, "DB_PATH", self.db):
            conn = SC.connect_ro()
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM candles")


class TestNotify(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = os.path.join(self.dir, "screen_notified.json")
        conn = make_db()
        for i, sym in enumerate("ABCDEFGHIJ"):
            add(conn, sym, zigzag(300, 1.0010 - 0.0002 * i, 0.01))
        add(conn, "OLD", zigzag(300, 1.0, 0.01), end_day=380)
        add(conn, "NEW", zigzag(50, 1.0, 0.01))
        self.sc = SC.compute(conn)
        conn.close()

    def test_push_text(self):
        text = SC.render_push(self.sc)
        lines = text.splitlines()
        self.assertIn("Ranking, not a validated signal.", lines[0])
        self.assertIn(self.sc["as_of"], lines[0])
        self.assertTrue(lines[1].startswith("1. A 12m "))
        self.assertIn("reversal risk", text)
        self.assertIn("stale, last bar", text)
        self.assertIn("skipped NEW", text)
        self.assertIn("Not a trade list.", lines[-1])
        self.assertLess(len(text.encode()), 4096)            # ntfy's message limit
        self.assertTrue(all(len(x) <= 80 for x in lines[1:-1]))

    def test_once_per_data_date(self):
        with unittest.mock.patch.object(SC.health, "send_ntfy", return_value=True) as push:
            self.assertTrue(SC.notify_once(self.sc, "t", self.state))
            self.assertFalse(SC.notify_once(self.sc, "t", self.state))
            self.assertEqual(push.call_count, 1)
            topic, title, body = push.call_args.args
            self.assertEqual((topic, title), ("t", SC.NTFY_TITLE))
            self.assertEqual(push.call_args.kwargs["priority"], "default")
            later = {**self.sc, "as_of": "2099-01-01"}
            self.assertTrue(SC.notify_once(later, "t", self.state))
            self.assertEqual(push.call_count, 2)

    def test_failed_push_is_retried(self):
        with unittest.mock.patch.object(SC.health, "send_ntfy", return_value=False):
            self.assertFalse(SC.notify_once(self.sc, "t", self.state))
        self.assertFalse(os.path.exists(self.state))
        with unittest.mock.patch.object(SC.health, "send_ntfy", return_value=True) as push:
            self.assertTrue(SC.notify_once(self.sc, "t", self.state))
            push.assert_called_once()

    def test_cli_without_topic_does_not_push(self):
        db = os.path.join(self.dir, "forge.db")
        conn = make_db(db)
        add(conn, "A", zigzag(300, 1.001, 0.01))
        conn.close()
        with unittest.mock.patch.object(SC.main, "DB_PATH", db), \
                unittest.mock.patch.object(SC.health, "send_ntfy") as push, \
                unittest.mock.patch("sys.stdout"):
            SC.main_cli(["--notify", "--ntfy-topic", "", "--no-json"])
            push.assert_not_called()
            SC.main_cli(["--notify", "--ntfy-topic", "t", "--no-json"])
            push.assert_called_once()


if __name__ == "__main__":
    unittest.main()
