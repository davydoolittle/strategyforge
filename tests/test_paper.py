"""
Tests for P4.1 paper reconciliation (paper.py) and engine.backtest's
"entries" list. Synthetic candles, a lineage strategy, and an in-memory
davesbridge signals table with alerts built from the engine's own entries.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import math
import os
import random
import sqlite3
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import engine  # noqa: E402
import lineage  # noqa: E402
import optimizer  # noqa: E402
import paper as P  # noqa: E402
import windows as W  # noqa: E402

H = 3600
T0 = 1_600_000_000 // H * H
N_BARS = 3000


def iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def received(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def loose_cfg():
    cfg = engine.default_cfg()
    for k in ("use_htf", "use_vwap", "use_volume", "avoid_chop", "avoid_late", "avoid_fake"):
        cfg[k] = False
    return cfg


def forge_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
                 " close REAL, volume REAL, PRIMARY KEY (pair, ts))")
    rng = random.Random(1)
    px = 100.0
    for i in range(N_BARS):
        o = px
        c = px * (1 + 0.004 * math.sin(i / 150) + rng.gauss(0, 0.006))
        conn.execute("INSERT INTO candles VALUES ('BTC-USD', ?, ?, ?, ?, ?, 100)",
                     (T0 + i * H, o, max(o, c) * 1.002, min(o, c) * 0.998, c))
        px = c
    optimizer.ensure_tables(conn)
    W.ensure_columns(conn)
    conn.execute("INSERT INTO candidates (id, run_at, pair, tf, params_json, passed, is_start,"
                 " split_ts, oos_end) VALUES (1, 'x', 'BTC-USD', '1h', ?, 1, ?, ?, ?)",
                 (json.dumps(loose_cfg()), iso(T0 + 300 * H), iso(T0 + 2000 * H),
                  iso(T0 + 2500 * H)))
    lineage.ensure_tables(conn)
    conn.execute("INSERT INTO strategies (id, name, candidate_id, pair, tf, params_json, status)"
                 " VALUES (1, 'c1-btc-1h', 1, 'BTC-USD', '1h', ?, 'backtest_winner')",
                 (json.dumps(loose_cfg()),))
    conn.commit()
    return conn


def signals_db(payloads):
    """payloads: [(received_ts, dict or raw str)]"""
    s = sqlite3.connect(":memory:")
    s.execute("CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT,"
              " dedup_hash TEXT, raw_payload TEXT, status TEXT)")
    for t, p in payloads:
        s.execute("INSERT INTO signals (received_at, dedup_hash, raw_payload, status)"
                  " VALUES (?, 'h', ?, 'logged_dry_run')",
                  (received(t), p if isinstance(p, str) else json.dumps(p, sort_keys=True)))
    s.commit()
    return s


def alert(bar_ts, side, price, name="SF c1-btc-1h", symbol="BTCUSD"):
    # fires on the close of bar_ts, reaches davesbridge 7 s later
    return (bar_ts + H + 7, {"strategy": name, "side": side, "symbol": symbol, "tf": "60",
                             "price": price})


class TestEngineEntries(unittest.TestCase):
    def test_entries_cover_every_trade_signal(self):
        conn = forge_db()
        self.addCleanup(conn.close)
        r = engine.backtest(conn, "BTC-USD", loose_cfg(), iso(T0), iso(T0 + (N_BARS - 1) * H))
        ent = {e["ts"]: e["side"] for e in r["entries"]}
        self.assertGreater(len(ent), 20)
        for t in r["trades"]:
            self.assertEqual(ent[t["signal_ts"]], t["dir"])


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.conn = forge_db()
        self.addCleanup(self.conn.close)
        strat = P.strategy_by_name(self.conn, "SF c1-btc-1h")
        self.ent = sorted(P.engine_entries(self.conn, strat, T0 + (N_BARS - 1) * H).values(),
                          key=lambda e: e["ts"])
        self.assertGreater(len(self.ent), 10)
        taken = {e["ts"] for e in self.ent}
        self.quiet = next(t for t in range(T0 + 1000 * H, T0 + N_BARS * H, H)
                          if t not in taken and t - H not in taken and t + H not in taken)

    def rows(self):
        cur = self.conn.execute("SELECT * FROM paper_trades ORDER BY signal_id")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur]

    def test_full_cycle(self):
        e1, e2, e3 = self.ent[-3], self.ent[-2], self.ent[-1]
        flip = "short" if e2["side"] == "long" else "long"
        sconn = signals_db([
            (T0, '{"action": "buy", "price": "123.45", "ticker": "TEST"}'),   # manual test
            alert(e1["ts"], e1["side"], e1["close"] * 1.0003),                # agrees
            alert(e2["ts"], flip, e2["close"]),                               # wrong side
            alert(self.quiet, "long", 100.0),                                 # engine silent
            alert(e3["ts"], e3["side"], e3["close"], symbol="ETHUSD"),        # agrees, odd symbol
            alert(e1["ts"], "long", 1.0, name="SF nobody"),                   # unknown name
            alert(T0 + (N_BARS + 5) * H, "long", 1.0),                        # no candles yet
        ])
        self.addCleanup(sconn.close)
        s = P.reconcile(self.conn, sconn, now="t")
        self.assertEqual(s["skipped_no_strategy_field"], 1)
        self.assertEqual(s["waiting_for_candles"], 1)
        self.assertEqual(s["unknown"], 1)
        self.assertEqual(s["new_rows"], 5)
        r = {x["signal_id"]: x for x in self.rows()}
        self.assertEqual(r[2]["engine_fired"], 1)
        self.assertEqual(r[2]["bar_ts"], e1["ts"])
        self.assertAlmostEqual(r[2]["price_engine"], e1["close"])
        self.assertIsNone(r[2]["note"])
        self.assertEqual(r[3]["engine_fired"], 0)
        self.assertIn(f"engine entered {e2['side']}", r[3]["note"])
        self.assertEqual(r[4]["engine_fired"], 0)
        self.assertIsNotNone(r[4]["price_engine"])                          # candle close
        self.assertEqual(r[5]["engine_fired"], 1)
        self.assertIn("symbol ETHUSD != BTCUSD", r[5]["note"])
        self.assertIsNone(r[6]["strategy_id"])
        self.assertIn("nobody", r[6]["note"])
        self.assertNotIn(7, r)                                              # waiting
        # engine entries in the reconciled span (quiet bar .. e3) that got no alert;
        # alerted bars are excluded even when the alert disagreed (e2)
        alerted = {e1["ts"], e2["ts"], e3["ts"], self.quiet}
        self.assertEqual([e["ts"] for e in s["engine_only"][1]],
                         [e["ts"] for e in self.ent if self.quiet <= e["ts"] <= e3["ts"]
                          and e["ts"] not in alerted])
        # idempotent
        s2 = P.reconcile(self.conn, sconn, now="t2")
        self.assertEqual(s2["new_rows"], 0)
        self.assertEqual(s2["waiting_for_candles"], 1)

    def test_engine_only_lists_missed_alerts(self):
        first, last = self.ent[-6], self.ent[-1]
        sconn = signals_db([alert(first["ts"], first["side"], first["close"]),
                            alert(last["ts"], last["side"], last["close"])])
        self.addCleanup(sconn.close)
        s = P.reconcile(self.conn, sconn)
        self.assertEqual([e["ts"] for e in s["engine_only"][1]], [e["ts"] for e in self.ent[-5:-1]])

    def test_offset_note(self):
        e = self.ent[-1]
        sconn = signals_db([alert(e["ts"] - H, e["side"], e["close"])])    # alert one bar early
        self.addCleanup(sconn.close)
        P.reconcile(self.conn, sconn)
        row = self.rows()[0]
        self.assertEqual(row["engine_fired"], 0)
        self.assertIn("engine fired same side +1 bar", row["note"])

    def test_no_alerts_report(self):
        sconn = signals_db([])
        self.addCleanup(sconn.close)
        s = P.reconcile(self.conn, sconn)
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            P.print_report(self.conn, s)
        self.assertIn("Nothing is deployed", out.getvalue())


if __name__ == "__main__":
    unittest.main()
