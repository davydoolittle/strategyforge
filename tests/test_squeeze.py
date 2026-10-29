"""
Tests for P6.2: the `squeeze` strategy family (app/strategies/squeeze.py) and
its path through the optimizer, ablation, sensitivity, lineage and Pine
export. Synthetic candles; no forge.db needed.
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
import indicators as ind  # noqa: E402
import lineage  # noqa: E402
import optimizer  # noqa: E402
import pine_export  # noqa: E402
import strategies  # noqa: E402
import validation as V  # noqa: E402
from strategies import squeeze as SQ  # noqa: E402

H = 3600
T0 = 1_600_000_000 // H * H
N_BARS = 4000        # the 90-day percentile needs 2,160 bars before any signal
SQZ = strategies.get("squeeze")


def iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def candles_db():
    """Quiet stretches (vol 0.2%) between loud ones (vol 1.2%) on a slow
    drift, so compression, breakouts and both range extremes all occur."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
                 " close REAL, volume REAL, PRIMARY KEY (pair, ts))")
    rng = random.Random(11)
    px = 100.0
    for i in range(N_BARS):
        quiet = (i // 150) % 3 == 0
        o = px
        r = 0.0015 * math.sin(i / 300) + rng.gauss(0, 0.002 if quiet else 0.012)
        c = px * (1 + r)
        conn.execute("INSERT INTO candles VALUES ('BTC-USD', ?, ?, ?, ?, ?, ?)",
                     (T0 + i * H, o, max(o, c) * 1.001, min(o, c) * 0.999, c, 100.0))
        px = c
    conn.commit()
    return conn


def loose():
    return {**SQZ.default_cfg(), "compression_pctile": 40.0, "breakout_len": 12,
            "extreme_bias": 0.5}


class TestSqueezeDefinition(unittest.TestCase):
    def test_six_searched_parameters(self):
        searched = set(SQZ.numeric_params) | set(SQZ.toggle_params)
        self.assertEqual(searched, {"compression_pctile", "breakout_len", "extreme_bias",
                                    "stop_atr", "rr_ratio", "use_shorts"})
        a, b = SQZ.sample_cfg(random.Random(1)), SQZ.sample_cfg(random.Random(2))
        varied = {k for k in a if a[k] != SQZ.default_cfg()[k] or a[k] != b[k]}
        self.assertTrue(varied <= searched, varied - searched)

    def test_nothing_from_v41(self):
        cfg = SQZ.default_cfg()
        for k in ("ema_fast", "ema_mid", "ema_slow", "adx_threshold", "min_rel_vol",
                  "max_dist_ema", "use_vwap", "use_trailing", "use_htf", "rsi_len"):
            self.assertNotIn(k, cfg)
        self.assertEqual(cfg["family"], "squeeze")

    def test_dna_differs_from_other_families(self):
        cfg = SQZ.default_cfg()
        self.assertNotEqual(lineage.dna_of(cfg), lineage.dna_of({**cfg, "family": "core"}))

    def test_sampler_is_deterministic_and_in_range(self):
        for seed in range(50):
            a = SQZ.sample_cfg(random.Random(seed))
            self.assertEqual(a, SQZ.sample_cfg(random.Random(seed)))
            self.assertTrue(SQZ.cfg_ok(a))
            self.assertTrue(5 <= a["compression_pctile"] <= 50)
            self.assertTrue(10 <= a["breakout_len"] <= 72)
            self.assertTrue(0.5 <= a["extreme_bias"] <= 0.95)   # always "nearer the extreme"
            self.assertTrue(a["use_longs"])


class TestSqueezeSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = candles_db()
        ts, o, h, l, c, v = engine.load_candles(cls.conn, "BTC-USD", T0 + N_BARS * H)
        cls.bars = {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v,
                    "atr": ind.atr(h, l, c, 14), "from_i": engine.WARMUP_BARS}
        cls.fc = features.FeatureComputer(cls.conn, "BTC-USD", H)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_pctile_equals_features_py_on_every_bar(self):
        pct = SQ.bb_bw_pctile(self.bars["close"], 24)
        self.assertIsNone(pct[2159])
        checked = 0
        for i in range(2100, N_BARS, 7):
            self.assertEqual(pct[i], self.fc.ohlcv_features(i)["bb_bw_pctile"], i)
            checked += 1
        self.assertGreater(checked, 200)

    def test_every_signal_meets_every_gate(self):
        cfg = loose()
        out = SQZ.signals(self.bars, cfg)
        c, h, l = self.bars["close"], self.bars["high"], self.bars["low"]
        n_long = n_short = 0
        for i, (lg, sh) in enumerate(out):
            if not (lg or sh):
                continue
            f = self.fc.ohlcv_features(i)
            self.assertLessEqual(f["bb_bw_pctile"], cfg["compression_pctile"])
            span = f["dist_30d_high_pct"] + f["dist_30d_low_pct"]
            pos = f["dist_30d_low_pct"] / span
            n = cfg["breakout_len"]
            if lg:
                n_long += 1
                self.assertGreater(c[i], max(h[i - n:i]))
                self.assertGreaterEqual(pos, cfg["extreme_bias"] - 1e-9)
            if sh:
                n_short += 1
                self.assertLess(c[i], min(l[i - n:i]))
                self.assertLessEqual(pos, 1 - cfg["extreme_bias"] + 1e-9)
        self.assertGreater(n_long, 0)
        self.assertGreater(n_short, 0)

    def test_no_signal_before_90_days(self):
        out = SQZ.signals(self.bars, {**loose(), "compression_pctile": 100.0, "extreme_bias": 0.0})
        self.assertFalse(any(map(any, out[:2160])))
        self.assertTrue(any(map(any, out[2160:])))

    def test_each_gate_binds(self):
        base = sum(map(any, SQZ.signals(self.bars, loose())))
        self.assertEqual(sum(map(any, SQZ.signals(self.bars, {**loose(), "extreme_bias": 1.01}))), 0)
        for k, (off, _, _) in SQZ.traps.items():
            self.assertGreater(sum(map(any, SQZ.signals(self.bars, {**loose(), k: off}))), base, k)

    def test_direction_toggles(self):
        out = SQZ.signals(self.bars, {**loose(), "use_shorts": False})
        self.assertFalse(any(sh for _, sh in out))
        out = SQZ.signals(self.bars, {**loose(), "use_longs": False})
        self.assertFalse(any(lg for lg, _ in out))

    def test_no_signal_uses_a_later_bar(self):
        full = SQZ.signals(self.bars, loose())
        cut = 3000
        short = {k: (v[:cut] if isinstance(v, list) else v) for k, v in self.bars.items()}
        self.assertEqual(SQZ.signals(short, loose()), full[:cut])


class TestSqueezeThroughThePipeline(unittest.TestCase):
    def setUp(self):
        self.conn = candles_db()
        self.addCleanup(self.conn.close)

    def test_engine_fixed_stop_and_target_only(self):
        r = engine.backtest(self.conn, "BTC-USD", loose(), iso(T0), iso(T0 + (N_BARS - 1) * H))
        self.assertGreater(r["n_trades"], 5)
        self.assertTrue({t["reason"] for t in r["trades"]} <= {"stop", "target", "opposite"})

    def test_optimizer_stores_family(self):
        optimizer.ensure_tables(self.conn)
        with mock.patch.dict(optimizer.collector.TICK_SIZE, {"BTC-USD": 0.001}):
            out = optimizer.run_pair(self.conn, "BTC-USD", "1h", 4, 61, family="squeeze")
        self.assertIsNotNone(out)
        rows = self.conn.execute("SELECT family, params_json, dna FROM candidates").fetchall()
        self.assertEqual(len(rows), 4)
        for fam, pj, dna in rows:
            self.assertEqual(fam, "squeeze")
            self.assertEqual(json.loads(pj)["tick_size"], 0.001)
            self.assertEqual(dna, lineage.dna_of(json.loads(pj)))
        self.assertEqual(V.sharpe_variance_from_candidates(self.conn, "BTC-USD", "1h",
                                                           "squeeze")[2], 4)
        self.assertEqual(V.sharpe_variance_from_candidates(self.conn, "BTC-USD", "1h", "core")[2], 0)

    def test_optimizer_resumes_an_interrupted_run(self):
        optimizer.ensure_tables(self.conn)
        optimizer.run_pair(self.conn, "BTC-USD", "1h", 3, 61, family="squeeze")
        optimizer.run_pair(self.conn, "BTC-USD", "1h", 5, 61, family="squeeze")
        rows = self.conn.execute("SELECT trial, dna FROM candidates ORDER BY trial").fetchall()
        self.assertEqual([t for t, _ in rows], [0, 1, 2, 3, 4])     # no trial stored twice
        fresh = candles_db()
        self.addCleanup(fresh.close)
        optimizer.ensure_tables(fresh)
        optimizer.run_pair(fresh, "BTC-USD", "1h", 5, 61, family="squeeze")
        self.assertEqual(rows, fresh.execute("SELECT trial, dna FROM candidates"
                                             " ORDER BY trial").fetchall())

    def test_ablation_and_sensitivity_use_squeeze_lists(self):
        cfg = loose()
        vs = ablation.variants(cfg)
        self.assertEqual([v[0] for v in vs], ["use_shorts", "compression_pctile", "extreme_bias"])
        sens = V.param_sensitivity(self.conn, "BTC-USD", cfg, H, iso(T0 + 2500 * H),
                                   iso(T0 + (N_BARS - 1) * H), pct=(0.25,))
        self.assertTrue(sens["neighbours"])
        self.assertEqual({r["param"] for r in sens["neighbours"]} - set(SQZ.sensitivity_params),
                         set())
        for r in sens["neighbours"]:
            if r["param"] == "breakout_len":
                self.assertIsInstance(r["to"], int)
            if r["param"] == "extreme_bias":
                self.assertLessEqual(r["to"], 1.0)

    def test_pine_export(self):
        script = pine_export.render(loose(), "SF test squeeze", "note")
        self.assertNotIn("{", script.replace("{{", "").split("alert(")[0])
        for gone in ("ema", "ta.dmi", "vwap", "useTrailing", "volMa"):
            self.assertNotIn(gone, script)
        self.assertIn("compPct       = input.float(40.0,", script)
        self.assertIn("breakoutLen   = input.int(12,", script)
        self.assertIn("ta.percentrank(bbBw, pctLen)", script)
        self.assertIn("ta.stdev(close, 20, false)", script)
        self.assertIn('"strategy":"SF test squeeze"', script)


if __name__ == "__main__":
    unittest.main()
