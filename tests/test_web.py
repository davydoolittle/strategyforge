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

import fast_tmp  # noqa: E402,F401  (temp files in RAM, see fast_tmp.py)

import adversary  # noqa: E402
import engine  # noqa: E402
import health as HEALTH  # noqa: E402
import lineage  # noqa: E402
import optimizer  # noqa: E402
import paper  # noqa: E402
import web  # noqa: E402
import windows as W  # noqa: E402
from test_squeeze import candles_db  # noqa: E402


TOKEN = "t" * 24


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
              "full": {"pf": float("inf")}, "hard_fails": ["A2"], "soft_fails": []}
    mem.execute("INSERT INTO adversarial_reports (run_at, candidate_id, pair, tf, verdict, hard_fails,"
                " soft_fails, report_json) VALUES ('2026-10-29', 2, 'BTC-USD', '1h', 'REJECTED',"
                " 1, 0, ?)", (json.dumps(report, allow_nan=True),))
    mem.execute("INSERT INTO strategies (name, created_at, candidate_id, pair, tf, dna, params_json,"
                " status, family) VALUES ('s1', '2026-10-29', 2, 'BTC-USD', '1h', 'x', '{}',"
                " 'backtest_winner', 'squeeze')")
    mem.execute("INSERT INTO strategies (name, created_at, candidate_id, pair, tf, dna, params_json,"
                " status, family, forward_test) VALUES ('s2', '2026-10-30', 3, 'BTC-USD', '1h', 'y',"
                " '{}', 'backtest_winner', 'squeeze', 1)")
    mem.execute("INSERT INTO health (strategy_id, day, cusum, alert) VALUES (1, '2026-10-28', -0.5, 0)")
    mem.execute("INSERT INTO health (strategy_id, day, cusum, alert, n_forward) VALUES (2, '2026-10-31', 0, 0, 3)")
    mem.execute("INSERT INTO health (strategy_id, day, cusum, alert, n_forward) VALUES (2, '2026-11-01', 0, 0, 4)")
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
        cls.srv = web.make_server("127.0.0.1", 0, cls.db, TOKEN)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, method="GET", token=TOKEN):
        req = urllib.request.Request(self.base + path, method=method,
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
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
        self.assertEqual((body[0]["forward_test"], body[0]["n_forward"]), (False, None))
        self.assertEqual((body[1]["forward_test"], body[1]["n_forward"], body[1]["forward_day"]),
                         (True, 4, "2026-11-01"))      # the latest health row

    def test_jobs(self):
        st, body = self.get("/api/jobs")
        self.assertEqual((st, body["jobs"]), (200, []))           # no jobs table: empty, not an error
        self.assertEqual(body["heartbeat"]["last_candle_ts"],
                         web.connect(self.db).execute("SELECT MAX(ts) FROM candles").fetchone()[0])
        self.assertTrue(body["heartbeat"]["stale"])              # synthetic candles are from 2020
        self.assertEqual(self.get("/api/jobs", token=None)[0], 401)

    def test_new_panels_empty_db(self):
        t = self.get("/api/tsp")[1]
        self.assertEqual((t["report"], t["my_ifts"], t["runs"]), (None, [], []))
        self.assertEqual(self.get("/api/patterns")[1], [])
        i = self.get("/api/insider")[1]
        self.assertEqual((i["clusters"], i["buys"], i["tag"]), ([], [], "UNTESTED, no prediction"))
        for path in ("/api/tsp", "/api/patterns", "/api/insider"):
            self.assertEqual(self.get(path, token=None)[0], 401)

    def test_tsp(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        import tsp_report
        tsp_report.ensure_tables(conn)
        conn.execute("INSERT INTO tsp_reports VALUES ('2026-10-29', 'proxy', 'x', 'old', 1)")
        conn.execute("INSERT INTO tsp_reports VALUES ('2026-10-30', 'tsp', 'y', 'new', 1)")
        conn.execute("INSERT INTO tsp_my_ifts (fill_date, target_json, kind) VALUES ('2026-10-01', '{\"C\": 100}', 'flexible')")
        cols = "run_at, data_mode, strategy, params_json, start, end, fill_lag, metrics_json"
        for at, mode, strat in (("2026-10-29", "proxy", "a"), ("2026-10-30", "tsp", "old"),
                                ("2026-11-01", "tsp", "b"), ("2026-11-01", "tsp", "c")):
            conn.execute(f"INSERT INTO tsp_runs ({cols}) VALUES (?, ?, ?, '{{}}', 's', 'e', 1, ?)",
                         (at, mode, strat, json.dumps({"cagr": 9.0, "max_dd": 20.0, "extra": 1})))
        d = web.tsp(conn)
        self.assertEqual(d["report"]["text"], "new")
        self.assertEqual(d["my_ifts"][0]["target"], {"C": 100})
        self.assertEqual(d["runs_mode"], "tsp")                 # official prices win over proxies
        self.assertEqual([r["strategy"] for r in d["runs"]], ["b", "c"])   # newest batch only
        self.assertEqual(d["runs"][0]["cagr"], 9.0)
        self.assertNotIn("extra", d["runs"][0])

    def test_patterns_order(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE pattern_results (pid TEXT, family TEXT, claim TEXT, asset TEXT, h INTEGER,"
                     " sign INTEGER, status TEXT, n INTEGER, n_base INTEGER, mean_on REAL, mean_base REAL,"
                     " hit_on REAL, hit_base REAL, p REAL, first_half REAL, second_half REAL,"
                     " first_date TEXT, active INTEGER, last_on TEXT, run_at TEXT)")
        for pid, st, p in (("a", "NOT SUPPORTED", 0.5), ("b", "SUGGESTIVE", 0.08), ("c", "TOO FEW", None),
                           ("d", "OPPOSITE", 0.01), ("e", "SUGGESTIVE", 0.02)):
            conn.execute("INSERT INTO pattern_results (pid, status, p) VALUES (?, ?, ?)", (pid, st, p))
        self.assertEqual([r["pid"] for r in web.patterns(conn)], ["e", "b", "d", "a", "c"])

    def test_insider_window_and_filters(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        web.IF.ensure_tables(conn)
        today = web.datetime(2026, 11, 2).date()
        txn = "INSERT INTO insider_txns (id, ticker, owner_name, code, value, txn_date, filing_date) VALUES (?, ?, 'o', ?, ?, ?, ?)"
        conn.executemany(txn, [
            (1, "AAA", "P", 500_000, "2026-10-20", "2026-10-22"),     # shown
            (2, "BBB", "P", 50_000, "2026-10-20", "2026-10-22"),      # under the size bar
            (3, "CCC", "S", 900_000, "2026-10-20", "2026-10-22"),     # a sale: not shown
            (4, "COIN", "P", 200_000, "2026-10-30", "2026-11-01"),    # shown, newest first
            (5, "DDD", "P", 900_000, "2026-06-01", "2026-06-03")])    # older than 90 days
        conn.execute("INSERT INTO insider_clusters (cluster_key, ticker, window_start, window_end, insider_count,"
                     " total_value) VALUES ('k1', 'AAA', '2026-10-01', '2026-10-08', 3, 1e6),"
                     " ('k2', 'OLD', '2026-05-01', '2026-05-08', 2, 1e6)")
        d = web.insider(conn, today=today)
        self.assertEqual([b["ticker"] for b in d["buys"]], ["COIN", "AAA"])
        self.assertEqual([c["ticker"] for c in d["clusters"]], ["AAA"])
        self.assertIn("COIN", d["crypto_linked"])
        self.assertEqual(d["last_filing"], "2026-11-01")

    def test_jobs_due_and_overdue(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE jobs (name TEXT PRIMARY KEY, cron_like_interval TEXT, command TEXT,"
                     " enabled INTEGER DEFAULT 1, last_run INTEGER, last_exit INTEGER,"
                     " last_duration_s REAL, last_error TEXT)")
        mon = 1793577600                                   # Mon 2026-11-02 00:00 UTC
        conn.executemany("INSERT INTO jobs VALUES (?, ?, 'x.py', ?, ?, ?, 1.0, ?)", [
            ("hourly", "1h", 1, mon, 0, None),
            ("late", "1h", 1, mon - 4 * 3600, 0, None),
            ("paused", "1h", 0, mon - 4 * 3600, 0, None),
            ("broke", "1d", 1, mon, 2, "x" * 1000),
            ("weekly", "mon@06:00", 1, mon - 86400, 0, None),  # Sunday: next slot Mon 06:00
            ("new", "1d", 1, None, None, None)])
        j = {r["name"]: r for r in web.jobs(conn, now=mon + 60)["jobs"]}
        self.assertEqual(j["hourly"]["next_due"], mon + 3600)
        self.assertFalse(j["hourly"]["overdue"])
        self.assertTrue(j["late"]["overdue"])
        self.assertFalse(j["paused"]["overdue"])
        self.assertTrue(j["broke"]["failed"])
        self.assertEqual(len(j["broke"]["last_error"]), 300)
        self.assertEqual(j["weekly"]["next_due"], mon + 6 * 3600)
        self.assertTrue(j["new"]["overdue"])
        self.assertIsNone(j["new"]["next_due"])

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
        self.assertEqual((body["hard_fails"], body["n_hard_fails"]), (["A2"], 1))
        self.assertEqual((body["soft_fails"], body["n_soft_fails"]), ([], 0))
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

    def test_screen(self):
        st, body = self.get("/api/screen")
        self.assertEqual(st, 200)
        self.assertEqual((body["rows"], body["note"]), ([], "ranking, not a validated signal"))
        self.assertIn("no screen yet", body["missing"])
        path = os.path.join(self.tmp, "screen.json")
        with open(path, "w") as f:
            json.dump({"note": "ranking, not a validated signal", "rows": [{"symbol": "SPY", "rank": 1}]}, f)
        self.addCleanup(os.remove, path)
        self.assertEqual(self.get("/api/screen")[1]["rows"], [{"symbol": "SPY", "rank": 1}])
        self.assertEqual(self.get("/api/screen", token=None)[0], 401)

    def test_read_only(self):
        self.assertEqual(self.get("/api/health", "POST")[0], 405)
        self.assertEqual(self.get("/api/health", "DELETE")[0], 405)
        self.assertEqual(self.get("/api/nothing")[0], 404)
        conn = web.connect(self.db)
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM health")

    def test_page(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            body = r.read().decode()
            self.assertEqual(r.headers["Content-Type"], "text/html; charset=utf-8")
            self.assertIn("script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com",
                          r.headers["Content-Security-Policy"])
        self.assertIn("/api/candidates?", body)
        self.assertIn("cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/", body)
        self.assertNotIn("innerHTML", body)
        self.assertIn("/api/jobs", body)
        self.assertIn("<option>1d</option>", body)
        for path in ("/api/tsp", "/api/patterns", "/api/insider"):
            self.assertIn(path, body)
        self.assertIn("UNTESTED, no prediction", body)              # static, not from the API
        self.assertIn("History, not advice", body)
        self.assertIn("You place every IFT yourself", body)
        self.assertIn("ranking, not a validated signal", body)  # the Screen panel's caveat is static          # DB strings go in as text, never as markup

    def test_auth(self):
        for tok in (None, "wrong" * 5, TOKEN + "x", TOKEN[:-1]):
            st, body = self.get("/api/candidates", token=tok)
            self.assertEqual(st, 401, tok)
            self.assertEqual(body, {"error": "bearer token required"})
        req = urllib.request.Request(self.base + "/api/health", headers={"Authorization": TOKEN})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)              # no "Bearer " scheme
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(cm.exception.headers["WWW-Authenticate"], 'Bearer realm="strategyforge"')
        self.assertEqual(self.get("/api/health", "POST", token=None)[0], 405)   # refused either way
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:   # the page needs no token
            self.assertEqual(r.status, 200)

    def test_refuses_to_start_without_a_token(self):
        for tok in (None, "", "short"):
            with self.assertRaises(SystemExit):
                web.make_server("127.0.0.1", 0, self.db, tok)

    def test_equity_curve(self):
        pts = web.equity_curve([{"exit_ts": 3, "pnl": -50.0}, {"exit_ts": 2, "pnl": 100.0}], 1000, 1)
        self.assertEqual([p["equity"] for p in pts], [1000, 1100, 1050])
        self.assertAlmostEqual(pts[-1]["dd_pct"], 100 * (1050 / 1100 - 1))

    def test_missing_db(self):
        srv = web.make_server("127.0.0.1", 0, os.path.join(self.tmp, "nope.db"), TOKEN)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{srv.server_address[1]}/api/health",
                headers={"Authorization": f"Bearer {TOKEN}"}), timeout=10)
            self.fail("expected 503")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 503)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope.db")))


if __name__ == "__main__":
    unittest.main()
