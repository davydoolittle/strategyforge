"""
Tests for P3.2 feature promotion (feature_evals.py): gate, dedup, holdout
on trades signalled after the cutoff, decide-once, and --annotate-new.
Synthetic trade_features rows; no candles needed except for annotate_new,
where the backtest is faked.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import os
import random
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import engine  # noqa: E402
import feature_evals as FE  # noqa: E402
import features  # noqa: E402
import optimizer  # noqa: E402

H = 3600
CUT = 1_000 * H


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        features.ensure_tables(self.conn)
        FE.ensure_tables(self.conn)
        self.rng = random.Random(4)
        self.next_sig = 0

    def add(self, n, sep=1.0, start=None, cid=1, pair="BTC-USD", flip=False):
        """n trades; feature x is higher in winners by `sep` (lower if flip),
        y is noise. Signals are one hour apart from `start`."""
        sig = self.next_sig if start is None else start
        for _ in range(n):
            won = self.rng.random() < 0.5
            x = self.rng.gauss((sep if won else 0.0) * (-1 if flip else 1), 1.0)
            self.conn.execute(
                "INSERT INTO trade_features (candidate_id, pair, tf_seconds, signal_ts, entry_ts,"
                " direction, pnl, reason, features_json) VALUES (?, ?, 3600, ?, ?, 'long', ?, 'x', ?)",
                (cid, pair, sig, sig + H, 10.0 if won else -10.0,
                 json.dumps({"x": x, "y": self.rng.gauss(0, 1), "ctx": None})))
            sig += H
        self.next_sig = sig
        self.conn.commit()

    def by_name(self, res):
        return {r["feature"]: r for r in res}


class TestGate(Base):
    def test_separating_feature_passes_noise_does_not(self):
        self.add(300, start=CUT - 300 * H + H)
        res = self.by_name(FE.evaluate(self.conn, at="t0"))
        self.assertEqual(res["x"]["status"], "awaiting_holdout")
        self.assertGreaterEqual(res["x"]["auc"], 0.6)
        self.assertEqual(res["x"]["suggested_op"], ">=")
        self.assertEqual(res["x"]["cutoff_ts"], CUT)
        self.assertEqual(res["y"]["status"], "below_gate")
        self.assertNotIn("ctx", res)                      # never has a value
        rows = self.conn.execute("SELECT feature, kind, measured_at FROM feature_evals").fetchall()
        self.assertEqual(rows, [("x", "first", "t0")])

    def test_low_auc_direction(self):
        self.add(300, flip=True)
        r = self.by_name(FE.evaluate(self.conn))["x"]
        self.assertLessEqual(r["auc"], 0.4)
        self.assertEqual(r["suggested_op"], "<=")

    def test_too_few_trades(self):
        self.add(150, sep=2.0)
        self.assertEqual(self.by_name(FE.evaluate(self.conn))["x"]["status"], "below_gate")

    def test_duplicates_count_once(self):
        self.add(150, sep=2.0, start=0)
        # the same 150 signals annotated by a second candidate: still 150
        rows = self.conn.execute("SELECT pair, tf_seconds, signal_ts, entry_ts, direction, pnl,"
                                 " reason, features_json FROM trade_features").fetchall()
        for r in rows:
            self.conn.execute("INSERT INTO trade_features (candidate_id, pair, tf_seconds, signal_ts,"
                              " entry_ts, direction, pnl, reason, features_json)"
                              " VALUES (2, ?, ?, ?, ?, ?, ?, ?, ?)", r)
        trades, n_raw = FE.load_trades(self.conn)
        self.assertEqual((len(trades), n_raw), (150, 300))
        self.assertEqual(self.by_name(FE.evaluate(self.conn))["x"]["status"], "below_gate")

    def test_dry_run_stores_nothing(self):
        self.add(300)
        FE.evaluate(self.conn, store=False)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM feature_evals").fetchone()[0], 0)

    def test_p_value(self):
        self.assertAlmostEqual(FE.auc_p_value(0.5, 50, 50), 0.5)
        self.assertLess(FE.auc_p_value(0.7, 100, 100), 0.001)


class TestHoldout(Base):
    def setUp(self):
        super().setUp()
        self.add(300, start=CUT - 300 * H + H)
        FE.evaluate(self.conn)

    def test_waits_then_promotes(self):
        self.add(30, start=CUT + H)
        r = self.by_name(FE.evaluate(self.conn))["x"]
        self.assertEqual((r["stage"], r["n"], r["status"]), ("holdout", 30, "awaiting_holdout"))
        self.add(40)
        r = self.by_name(FE.evaluate(self.conn))["x"]
        self.assertEqual(r["n"], 70)
        self.assertTrue(r["same_side"])
        self.assertEqual(r["status"], "candidate_filter")

    def test_old_trades_added_later_are_not_holdout(self):
        # a new candidate annotated over the SAME past: not new information
        self.add(100, start=0, cid=9, pair="ETH-USD")
        r = self.by_name(FE.evaluate(self.conn))["x"]
        self.assertEqual(r["n"], 0)

    def test_flip_rejects_and_decision_is_final(self):
        self.add(80, start=CUT + H, flip=True)
        r = self.by_name(FE.evaluate(self.conn))["x"]
        self.assertEqual(r["status"], "rejected")
        self.add(500, sep=2.0)                   # now it looks great again...
        r = self.by_name(FE.evaluate(self.conn))["x"]
        self.assertTrue(r["same_side"])
        self.assertEqual(r["status"], "rejected")   # ...but it was decided
        self.assertTrue(r["decided_earlier"])

    def test_first_row_is_not_remeasured(self):
        self.add(80, start=CUT + H)
        FE.evaluate(self.conn)
        firsts = self.conn.execute("SELECT COUNT(*) FROM feature_evals WHERE kind='first'").fetchone()[0]
        self.assertEqual(firsts, 1)


class TestAnnotateNew(unittest.TestCase):
    def test_only_trades_after_last_annotated(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        optimizer.ensure_tables(conn)
        features.ensure_tables(conn)
        conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
                     " close REAL, volume REAL)")
        conn.execute("INSERT INTO candles VALUES ('BTC-USD', ?, 1, 1, 1, 1, 1)", (50 * H,))
        conn.execute("INSERT INTO candidates (id, run_at, pair, tf, params_json, passed, is_start,"
                     " split_ts, oos_end) VALUES (5, 'x', 'BTC-USD', '1h', ?, 1,"
                     " '1970-01-01T00:00:00', '1970-01-01T10:00:00', '1970-01-01T20:00:00')",
                     (json.dumps(engine.default_cfg()),))
        conn.execute("INSERT INTO trade_features (candidate_id, pair, tf_seconds, signal_ts, pnl,"
                     " features_json) VALUES (5, 'BTC-USD', 3600, ?, 1, '{}')", (20 * H,))
        trades = [{"signal_ts": s * H} for s in (10, 20, 30, 40)]
        got = {}

        def fake_annotate(c, cid, pair, tf_s, ts):
            got["sigs"] = [t["signal_ts"] for t in ts]
            return len(ts)

        def fake_bt(c, pair, cfg, s, e, tf):
            got["window"] = (s, e)
            return {"trades": trades}
        with mock.patch.object(FE.engine, "backtest", fake_bt), \
                mock.patch.object(FE.features, "annotate_trades", fake_annotate):
            n = FE.annotate_new(conn, progress=False)
        self.assertEqual(n, 2)
        self.assertEqual(got["sigs"], [30 * H, 40 * H])
        self.assertEqual(got["window"], ("1970-01-01T00:00:00", "1970-01-03T02:00:00"))


if __name__ == "__main__":
    unittest.main()
