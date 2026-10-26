"""
Tests for P3.3 event fingerprinting (fingerprint.py): window labelling on
one grid, context deltas, store idempotence, and the AUC / multiple-
comparison report. Synthetic candles and context rows.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import io
import json
import os
import random
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import fingerprint as FP  # noqa: E402

H = 3600
T0 = 1_600_000_000 // H * H


class TestFindWindows(unittest.TestCase):
    def test_planted_jumps(self):
        c = [100.0] * 400
        atr = [1.0] * 400
        # grid starts at 0, 24, 48, ...: window [48, 72] jumps up 5, [144, 168] down 4
        for i in range(60, 400):
            c[i] += 5.0
        for i in range(150, 400):
            c[i] -= 9.0
        moves, nones = FP.find_windows(c, atr, start=0, horizon=24, k=3.0)
        self.assertEqual([(i, round(r)) for i, r in moves], [(48, 5), (144, -9)])
        starts = [i for i, _ in moves + nones]
        self.assertEqual(sorted(starts), list(range(0, 400 - 24, 24)))   # one grid, no overlap
        self.assertTrue(all(abs(r) < 3 for _, r in nones))

    def test_missing_atr_skipped(self):
        moves, nones = FP.find_windows([1.0] * 100, [None] * 100, start=0)
        self.assertEqual((moves, nones), ([], []))


class FakeFC:
    """Only what context_deltas needs: _ctx_at over an in-memory dict."""
    def __init__(self, polls):
        self.polls = polls      # {(metric, pair): [(ts, value)]}

    def _ctx_at(self, metric, pair, at_ts, max_age=7200):
        best = None
        for ts, v in self.polls.get((metric, pair), []):
            if ts <= at_ts and (best is None or ts > best[0]):
                best = (ts, v)
        if best is None or at_ts - best[0] > max_age:
            return None
        return best[1]


class TestContextDeltas(unittest.TestCase):
    def test_deltas_and_gaps(self):
        t = 100 * 86400
        fc = FakeFC({
            ("funding_rate", "BTC-USD"): [(t - 86400, 0.01), (t - 7 * 86400 - 3 * H, 0.03), (t, 0.02)],
            ("open_interest", "BTC-USD"): [(t - 30 * 86400, 200.0), (t, 250.0)],
            ("fear_greed", "GLOBAL"): [(t - 20 * H, 50.0)],          # 20h old: within 26h
        })
        d = FP.context_deltas(fc, "BTC-USD", t)
        self.assertAlmostEqual(d["funding_rate_chg_24h"], 0.01)
        self.assertAlmostEqual(d["funding_rate_chg_7d"], -0.01)     # 3h-old poll: within 4h
        self.assertIsNone(d["funding_rate_chg_30d"])
        self.assertAlmostEqual(d["open_interest_chg_30d"], 25.0)    # percent
        self.assertIsNone(d["open_interest_chg_24h"])
        self.assertIsNone(d["dvol_chg_24h"])                       # no feed at all
        self.assertIsNone(d["fear_greed_chg_24h"])                 # no poll 24h earlier
        self.assertEqual(len(d), len(FP.DELTA_NAMES))


def synthetic_db(n=3000, with_context=False):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
                  " close REAL, volume REAL, PRIMARY KEY (pair, ts))")
    conn.execute("CREATE TABLE context (ts INTEGER, metric TEXT, pair TEXT, value REAL,"
                 " PRIMARY KEY (ts, metric, pair))")
    rng = random.Random(2)
    px = 100.0
    for i in range(n):
        o = px
        c = px * (1 + rng.gauss(0, 0.008))
        conn.execute("INSERT INTO candles VALUES ('BTC-USD', ?, ?, ?, ?, ?, ?)",
                     (T0 + i * H, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 100.0))
        px = c
    if with_context:
        for i in range(n - 500, n):
            conn.execute("INSERT INTO context VALUES (?, 'funding_rate', 'BTC-USD', ?)",
                         (T0 + i * H, rng.gauss(0, 1)))
    return conn


class TestBuildStore(unittest.TestCase):
    def test_build_store_idempotent(self):
        conn = synthetic_db()
        self.addCleanup(conn.close)
        rows = FP.build(conn, "BTC-USD", ctx_start=FP.context_start(conn))
        self.assertGreater(len(rows), 50)
        self.assertTrue(any(r["label"] == "move" for r in rows))
        self.assertTrue(any(r["label"] == "none" for r in rows))
        self.assertTrue(all(r["end_ts"] - r["start_ts"] == 24 * H for r in rows))
        self.assertNotIn("funding_rate", rows[0]["features"])     # no context era: OHLCV only
        FP.store(conn, "BTC-USD", rows)
        FP.store(conn, "BTC-USD", rows)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0], len(rows))
        f = json.loads(conn.execute("SELECT features_json FROM fingerprints LIMIT 1").fetchone()[0])
        self.assertIn("vol_of_vol", f)

    def test_context_era_gets_context_features(self):
        conn = synthetic_db(with_context=True)
        self.addCleanup(conn.close)
        ctx = FP.context_start(conn)
        rows = FP.build(conn, "BTC-USD", ctx_start=ctx)
        era = [r for r in rows if r["start_ts"] >= ctx]
        self.assertTrue(era)
        self.assertTrue(all("funding_rate_chg_24h" in r["features"] for r in era))
        self.assertTrue(all("funding_rate" not in r["features"] for r in rows if r["start_ts"] < ctx))

    def test_context_start_without_table(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        self.assertIsNone(FP.context_start(conn))


def fake_rows(n_move=200, n_none=800, sep=1.0, ctx_from=None):
    rng = random.Random(8)
    rows = []
    for k in range(n_move + n_none):
        move = k < n_move
        f = {"signal": rng.gauss(sep if move else 0.0, 1.0), "noise": rng.gauss(0, 1)}
        ts = k * 24 * H
        if ctx_from is not None and ts >= ctx_from:
            f["funding_rate"] = rng.gauss(0, 1)
        rows.append({"pair": "BTC-USD", "start_ts": ts, "end_ts": ts + 24 * H,
                     "label": "move" if move else "none",
                     "direction": ("up" if k % 2 else "down") if move else None,
                     "move_atr": 5.0 if move else 1.0, "features": f})
    return rows


class TestAnalyse(unittest.TestCase):
    def test_planted_feature_found_noise_not(self):
        res = FP.analyse(fake_rows(), None)
        by = {t["feature"]: t for t in res["table_a"]}
        self.assertGreater(by["signal"]["all"]["auc"], 0.7)
        self.assertLess(by["signal"]["all"]["p"], 1e-6)
        self.assertLess(abs(by["noise"]["all"]["auc"] - 0.5), 0.08)
        self.assertEqual(res["table_b"], [])
        self.assertEqual(res["n_tests"], 6)                        # 2 features x all/up/down
        self.assertAlmostEqual(res["bonferroni_p"], 0.05 / 6)
        self.assertIn(("signal", "all", "A"), res["bonferroni_pass"])
        self.assertNotIn(("noise", "all", "A"), res["bonferroni_pass"])

    def test_context_feature_only_in_table_b(self):
        rows = fake_rows(ctx_from=0)
        res = FP.analyse(rows, 0)
        self.assertNotIn("funding_rate", [t["feature"] for t in res["table_a"]])
        self.assertIn("funding_rate", [t["feature"] for t in res["table_b"]])

    def test_small_class_gives_no_auc(self):
        res = FP.analyse(fake_rows(n_move=3), None)
        self.assertIsNone(res["table_a"][0]["all"]["auc"])

    def test_report_banners(self):
        rows = fake_rows(n_move=200, n_none=200)          # 50% moves: not rare
        out = io.StringIO()
        with redirect_stdout(out):
            FP.print_report(rows, FP.analyse(rows, None), 3.0, rows[-1]["end_ts"])
        text = out.getvalue()
        self.assertIn("ordinary volatility", text)
        self.assertIn("no context data", text)
        self.assertIn("Bonferroni", text)


if __name__ == "__main__":
    unittest.main()
