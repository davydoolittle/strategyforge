"""
Tests for the read-only dashboard API (P7.1). A temp-file SQLite database
(mode=ro needs a file) with synthetic candles, a few real optimizer
candidates and hand-made report / lineage / health / paper rows.
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
from unittest import mock

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "app"))
sys.path.insert(0, HERE)

import adversary  # noqa: E402
import engine  # noqa: E402
import health as HEALTH  # noqa: E402
import lineage  # noqa: E402
import optimizer  # noqa: E402
import paper  # noqa: E402
import web  # noqa: E402
import windows as W  # noqa: E402
from test_squeeze import candles_db  # noqa: E402


def build_db(path):
    mem = candles_db()
    optimizer.ensure_tables(mem)
    with mock.patch.dict(optimizer.collector.TICK_SIZE, {"BTC-USD": 0.001}):
        optimizer.run_pair(mem, "BTC-USD", "1h", 6, 61, family="squeeze")
    adversary.ensure_tables(mem)
    lineage.ensure_tables(mem)
    HEALTH.ensure_tables(mem)
    paper.ensure_tables(mem)
    report = {"attacks": {"A2": {"dsr_pair_tf": 0.12, "sharpe_per_trade": 0.2},
                          "A3": {"consistency": 0.6}, "A4": {"plateau_score": 0.8},
                          "A8": {"dd_p95_pct_of_capital": 20.0}},
              "full": {"pf": float("inf")}}
    mem.execute("INSERT INTO adversarial_reports (run_at, candidate_id, pair, tf, verdict, hard_fails,"
                " soft_fails, report_json) VALUES ('2026-10-29', 2, 'BTC-USD', '1h', 'REJECTED',"
                " '[\"A2\"]', '[]', ?)", (json.dumps(report, allow_nan=True),))
    mem.execute("INSERT INTO strategies (name, created_at, candidate_id, pair, tf, dna, params_json,"
                " status, family) VALUES ('s1', '2026-10-29', 2, 'BTC-USD', '1h', 'x', '{}',"
                " 'backtest_winner', 'squeeze')")
    mem.execute("INSERT INTO health (strategy_id, day, cusum, alert) VALUES (1, '2026-10-28', -0.5, 0)")
    mem.execute("INSERT INTO paper_summary (strategy_id, day, n_trades, net) VALUES (1, '2026-10-28', 2, 15.0)")
    mem.commit()
    disk = sqlite3.connect(path)
    mem.backup(disk)
    disk.close()
    mem.close()


class TestWeb(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.db = os.path.join(cls.tmp, "forge.db")
        build_db(cls.db)
        cls.srv = web.make_server("127.0.0.1", 0, cls.db)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, method="GET"):
        req = urllib.request.Request(self.base + path, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_strategies(self):
        st, body = self.get("/api/strategies")
        self.assertEqual(st, 200)
        self.assertEqual(body[0]["name"], "s1")
        self.assertEqual((body[0]["report_id"], body[0]["verdict"]), (1, "REJECTED"))

    def test_candidates_and_filters(self):
        st, body = self.get("/api/candidates")
        self.assertEqual(st, 200)
        self.assertEqual(len(body), 6)
        self.assertEqual(body[0]["id"], 2)                       # reported first
        self.assertEqual((body[0]["dsr"], body[0]["wf_consistency"], body[0]["plateau"]), (0.12, 0.6, 0.8))
        self.assertEqual(len(self.get("/api/candidates?reported=1")[1]), 1)
        self.assertEqual(len(self.get("/api/candidates?reported=0")[1]), 5)
        self.assertEqual(self.get("/api/candidates?tf=4h")[1], [])
        self.assertEqual(len(self.get("/api/candidates?limit=2")[1]), 2)
        passed = self.get("/api/candidates?passed=1")[1]
        self.assertTrue(all(c["passed"] == 1 for c in passed))
        self.assertEqual(self.get("/api/candidates?passed=maybe")[0], 400)

    def test_report_and_equity(self):
        st, body = self.get("/api/reports/1")
        self.assertEqual(st, 200)
        self.assertIsNone(body["report"]["full"]["pf"])          # inf -> null
        self.assertEqual(body["hard_fails"], ["A2"])
        self.assertNotIn("equity", body)
        st, body = self.get("/api/reports/1?equity=1")
        self.assertEqual(st, 200)
        eq = body["equity"]
        conn = sqlite3.connect(self.db)
        self.addCleanup(conn.close)
        is_start, split, end = W.for_candidate(conn, 2)
        pj = conn.execute("SELECT params_json FROM candidates WHERE id=2").fetchone()[0]
        cfg = W.pin_cfg(json.loads(pj), is_start, 3600)
        full = engine.backtest(conn, "BTC-USD", cfg, is_start, end, 3600)
        self.assertEqual(eq["n_trades"], full["n_trades"])
        self.assertAlmostEqual(eq["points"][-1]["equity"], cfg["initial_capital"] + full["net"])
        self.assertEqual(len(eq["points"]), full["n_trades"] + 1)
        self.assertTrue(all(p["dd_pct"] <= 0 for p in eq["points"]))
        self.assertEqual(self.get("/api/reports/99")[0], 404)

    def test_health_and_paper(self):
        self.assertEqual(self.get("/api/health")[1][0]["name"], "s1")
        body = self.get("/api/paper/1")[1]
        self.assertEqual(body["summary"][0]["net"], 15.0)
        self.assertEqual(body["reconciliation"], [])

    def test_read_only(self):
        self.assertEqual(self.get("/api/health", "POST")[0], 405)
        self.assertEqual(self.get("/api/health", "DELETE")[0], 405)
        self.assertEqual(self.get("/api/nothing")[0], 404)
        conn = web.connect(self.db)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM health")

    def test_equity_curve(self):
        pts = web.equity_curve([{"exit_ts": 3, "pnl": -50.0}, {"exit_ts": 2, "pnl": 100.0}], 1000, 1)
        self.assertEqual([p["equity"] for p in pts], [1000, 1100, 1050])
        self.assertAlmostEqual(pts[-1]["dd_pct"], 100 * (1050 / 1100 - 1))

    def test_missing_db(self):
        srv = web.make_server("127.0.0.1", 0, os.path.join(self.tmp, "nope.db"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{srv.server_address[1]}/api/health", timeout=10)
            self.fail("expected 503")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope.db")))


if __name__ == "__main__":
    unittest.main()
