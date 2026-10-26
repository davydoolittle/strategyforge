"""
Tests for P3.1 filter hooks: features.check_filters, engine.backtest with
cfg["filters"] on synthetic candles, and filters.py's lineage registration.
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
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import ablation  # noqa: E402
import engine  # noqa: E402
import features  # noqa: E402
import filters as F  # noqa: E402
import lineage  # noqa: E402
import optimizer  # noqa: E402
import pine_export  # noqa: E402

T0 = 1_600_000_000 // 3600 * 3600
N_BARS = 4000


def iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def synthetic_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
                 " close REAL, volume REAL, PRIMARY KEY (pair, ts))")
    conn.execute("CREATE TABLE context (ts INTEGER, source TEXT, metric TEXT, pair TEXT, value REAL)")
    rng = random.Random(1)
    px = 100.0
    for i in range(N_BARS):
        o = px
        c = px * (1 + 0.004 * math.sin(i / 150) + rng.gauss(0, 0.006))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.002)))
        lo = min(o, c) * (1 - abs(rng.gauss(0, 0.002)))
        conn.execute("INSERT INTO candles VALUES ('BTC-USD', ?, ?, ?, ?, ?, ?)",
                     (T0 + i * 3600, o, h, lo, c, rng.uniform(50, 150)))
        px = c
    return conn


def loose_cfg():
    cfg = engine.default_cfg()
    for k in ("use_htf", "use_vwap", "use_volume", "avoid_chop", "avoid_late", "avoid_fake"):
        cfg[k] = False
    return cfg


def hour(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).hour


class FakeFC:
    def __init__(self, f):
        self.f = f

    def at(self, ts, names=None):
        return self.f


class TestCheckFilters(unittest.TestCase):
    def test_ops(self):
        fc = FakeFC({"vol_of_vol": 15.0})
        self.assertTrue(features.check_filters(fc, [{"feature": "vol_of_vol", "op": ">=", "value": 12}], 0))
        self.assertFalse(features.check_filters(fc, [{"feature": "vol_of_vol", "op": "<=", "value": 12}], 0))
        both = [{"feature": "vol_of_vol", "op": ">=", "value": 12},
                {"feature": "vol_of_vol", "op": "<=", "value": 14}]
        self.assertFalse(features.check_filters(fc, both, 0))

    def test_none_blocks(self):
        fc = FakeFC({"funding_rate": None})
        self.assertFalse(features.check_filters(fc, [{"feature": "funding_rate", "op": ">=", "value": -1}], 0))

    def test_unknown_feature_raises(self):
        with self.assertRaises(KeyError):
            features.check_filters(FakeFC({}), [{"feature": "vol_of_voll", "op": ">=", "value": 1}], 0)

    def test_validate(self):
        with self.assertRaises(ValueError):
            features.validate_filters([{"feature": "x", "op": ">", "value": 1}])
        with self.assertRaises(ValueError):
            features.validate_filters([{"feature": "x", "op": ">="}])

    def test_parse(self):
        self.assertEqual(F.parse_filter(" vol_of_vol >= 12 "),
                         {"feature": "vol_of_vol", "op": ">=", "value": 12.0})
        self.assertEqual(F.parse_filter("funding_z30<=-1.5e0")["value"], -1.5)
        with self.assertRaises(ValueError):
            F.parse_filter("vol_of_vol > 12")


class TestEngineFilters(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = synthetic_db()
        cls.span = (iso(T0), iso(T0 + (N_BARS - 1) * 3600))
        cls.base = engine.backtest(cls.conn, "BTC-USD", loose_cfg(), *cls.span)

    def run_with(self, flts):
        return engine.backtest(self.conn, "BTC-USD", {**loose_cfg(), "filters": flts}, *self.span)

    def test_base_has_trades(self):
        self.assertGreater(self.base["n_trades"], 50)

    def test_empty_filters_identical(self):
        r = self.run_with([])
        self.assertEqual([t["signal_ts"] for t in r["trades"]],
                         [t["signal_ts"] for t in self.base["trades"]])

    def test_hour_filter_holds_at_signal_bar(self):
        r = self.run_with([{"feature": "hour", "op": "<=", "value": 11}])
        self.assertGreater(r["n_trades"], 0)
        self.assertLess(r["n_trades"], self.base["n_trades"])
        self.assertTrue(all(hour(t["signal_ts"]) <= 11 for t in r["trades"]))

    def test_impossible_filter_no_trades(self):
        self.assertEqual(self.run_with([{"feature": "hour", "op": ">=", "value": 99}])["n_trades"], 0)

    def test_context_filter_with_no_context_blocks_all(self):
        self.assertEqual(self.run_with([{"feature": "funding_rate", "op": ">=", "value": -1e9}])
                         ["n_trades"], 0)

    def test_filters_change_dna(self):
        c = loose_cfg()
        self.assertNotEqual(lineage.dna_of(c),
                            lineage.dna_of({**c, "filters": [{"feature": "hour", "op": "<=", "value": 11}]}))

    def test_pine_export_refuses(self):
        with self.assertRaises(SystemExit):
            pine_export.render({**loose_cfg(), "filters": [{"feature": "hour", "op": "<=", "value": 1}]},
                               "t", "n")


def fake_run_cfg(conn, pair, cfg, win, tf_s):
    n = 30 if cfg.get("filters") else 40
    trades = [{"signal_ts": i * 3600, "pnl": 10.0 if i % 3 else -5.0, "entry_ts": i * 3600,
               "exit_ts": i * 3600 + 1} for i in range(n)]
    r = {"n_trades": n, "net": sum(t["pnl"] for t in trades), "pf": 2.0, "max_dd": 5.0,
         "win_rate": 66.0, "trades": trades}
    return r, r


class TestRegister(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        optimizer.ensure_tables(self.conn)
        ablation.ensure_tables(self.conn)
        F.ensure_tables(self.conn)
        self.conn.execute(
            "INSERT INTO candidates (id, run_at, pair, tf, params_json, passed, is_start, split_ts,"
            " oos_end, windows_backfilled) VALUES (7, 'x', 'BTC-USD', '1h', ?, 1,"
            " '2024-01-01T00:00:00', '2025-01-01T00:00:00', '2025-06-01T00:00:00', 0)",
            (json.dumps(engine.default_cfg()),))
        p = mock.patch.object(ablation, "run_cfg", fake_run_cfg)
        p.start()
        self.addCleanup(p.stop)

    def test_child_candidate_and_lineage(self):
        rep, raw = F.compare(self.conn, 7, [F.parse_filter("vol_of_vol>=12")])
        self.assertEqual(rep["blocked"]["oos"]["n"], 10)
        with mock.patch("builtins.print"):
            cid, sid = F.register(self.conn, rep, raw)
        fo, pj = self.conn.execute("SELECT filter_of, params_json FROM candidates WHERE id=?",
                                   (cid,)).fetchone()
        self.assertEqual(fo, 7)
        self.assertEqual(json.loads(pj)["filters"],
                         [{"feature": "vol_of_vol", "op": ">=", "value": 12.0}])
        self.assertNotIn("history_start_ts", json.loads(pj))
        parent_sid, note = self.conn.execute("SELECT parent_id, note FROM strategies WHERE id=?",
                                             (sid,)).fetchone()
        self.assertEqual(note, "filter: vol_of_vol >= 12")
        self.assertEqual(self.conn.execute("SELECT candidate_id FROM strategies WHERE id=?",
                                           (parent_sid,)).fetchone()[0], 7)
        # idempotent: same filter again -> same rows, nothing new
        with mock.patch("builtins.print"):
            self.assertEqual(F.register(self.conn, rep, raw), (cid, sid))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM strategies").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
