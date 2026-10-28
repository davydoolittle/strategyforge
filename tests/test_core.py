"""
Tests for P5.3: the `core` strategy family (app/strategies/core.py) and its
path through the optimizer, ablation, sensitivity, lineage and Pine export.
Synthetic candles; no forge.db needed.
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
import indicators as ind  # noqa: E402
import lineage  # noqa: E402
import optimizer  # noqa: E402
import pine_export  # noqa: E402
import strategies  # noqa: E402
import validation as V  # noqa: E402
import windows as W  # noqa: E402

H = 3600
T0 = 1_600_000_000 // H * H
N_BARS = 2500
CORE = strategies.get("core")


def iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def candles_db():
    """Trending swings with volume bursts on big bars, so every core gate
    both passes and fails somewhere."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
                 " close REAL, volume REAL, PRIMARY KEY (pair, ts))")
    rng = random.Random(7)
    px = 100.0
    for i in range(N_BARS):
        o = px
        r = 0.006 * math.sin(i / 120) + rng.gauss(0, 0.007)
        c = px * (1 + r)
        vol = 100 * (1 + 3 * abs(r) / 0.007 * rng.random())
        conn.execute("INSERT INTO candles VALUES ('BTC-USD', ?, ?, ?, ?, ?, ?)",
                     (T0 + i * H, o, max(o, c) * 1.002, min(o, c) * 0.998, c, vol))
        px = c
    conn.commit()
    return conn


def loose():
    return {**CORE.default_cfg(), "adx_threshold": 15.0, "min_rel_vol": 0.9, "max_dist_ema": 3.0}


class TestCoreDefinition(unittest.TestCase):
    def test_at_most_ten_searched_parameters(self):
        a, b = CORE.sample_cfg(random.Random(1)), CORE.sample_cfg(random.Random(2))
        searched = set(CORE.numeric_params) | set(CORE.toggle_params)
        self.assertLessEqual(len(searched), 10)
        # every key the sampler varies is one of them
        varied = {k for k in a if a[k] != CORE.default_cfg()[k] or a[k] != b[k]}
        self.assertTrue(varied <= searched, varied - searched)

    def test_removed_v41_parts_are_gone(self):
        cfg = CORE.default_cfg()
        for k in ("use_vwap", "avoid_chop", "max_compression", "max_big_bars", "lookback_big",
                  "use_trailing", "trail_atr", "use_htf", "rsi_len", "min_ema_sep"):
            self.assertNotIn(k, cfg)
        self.assertEqual(cfg["family"], "core")

    def test_dna_never_collides_with_v41(self):
        v41 = {k: v for k, v in CORE.default_cfg().items() if k != "family"}
        self.assertNotEqual(lineage.dna_of(CORE.default_cfg()), lineage.dna_of(v41))

    def test_sampler_is_deterministic_and_ordered(self):
        for seed in range(50):
            a = CORE.sample_cfg(random.Random(seed))
            self.assertEqual(a, CORE.sample_cfg(random.Random(seed)))
            self.assertTrue(CORE.cfg_ok(a))
            self.assertTrue(15 <= a["adx_threshold"] <= 35)
            self.assertTrue(10 <= a["vol_ma_len"] <= 40)
            self.assertTrue(a["use_longs"])


class TestCoreSignals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = candles_db()
        ts, o, h, l, c, v = engine.load_candles(cls.conn, "BTC-USD", T0 + N_BARS * H)
        cls.bars = {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v,
                    "atr": ind.atr(h, l, c, 14), "from_i": engine.WARMUP_BARS}

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_every_signal_meets_every_gate(self):
        cfg = loose()
        out = CORE.signals(self.bars, cfg)
        b = self.bars
        c, o, h, l, v = b["close"], b["open"], b["high"], b["low"], b["volume"]
        ef, em, es = (ind.ema(c, cfg[k]) for k in ("ema_fast", "ema_mid", "ema_slow"))
        adx = ind.dmi(h, l, c, 14, 14)
        hi, lo = ind.donchian_prev(h, l, 20)
        vma = ind.sma(v, cfg["vol_ma_len"])
        n_long = n_short = 0
        for i, (lg, sh) in enumerate(out):
            if not (lg or sh):
                continue
            self.assertGreater(adx[i], cfg["adx_threshold"])
            self.assertGreaterEqual(v[i] / vma[i], cfg["min_rel_vol"])
            self.assertLessEqual(abs(c[i] - ef[i]) / ef[i] * 100, cfg["max_dist_ema"])
            if lg:
                n_long += 1
                self.assertTrue(ef[i] > em[i] > es[i] and c[i] > hi[i] and c[i] > o[i]
                                and c[i] > h[i - 1])
            if sh:
                n_short += 1
                self.assertTrue(ef[i] < em[i] < es[i] and c[i] < lo[i] and c[i] < o[i]
                                and c[i] < l[i - 1])
        self.assertGreater(n_long, 0)
        self.assertGreater(n_short, 0)

    def test_each_gate_binds(self):
        base = sum(map(any, CORE.signals(self.bars, loose())))
        for k, off in (("adx_threshold", 99.0), ("min_rel_vol", 99.0), ("max_dist_ema", 0.0)):
            n = sum(map(any, CORE.signals(self.bars, {**loose(), k: off})))
            self.assertEqual(n, 0, k)
        for k, (off, _, _) in CORE.traps.items():
            self.assertGreaterEqual(sum(map(any, CORE.signals(self.bars, {**loose(), k: off}))),
                                    base, k)

    def test_direction_toggles(self):
        out = CORE.signals(self.bars, {**loose(), "use_shorts": False})
        self.assertFalse(any(sh for _, sh in out))
        out = CORE.signals(self.bars, {**loose(), "use_longs": False})
        self.assertFalse(any(lg for lg, _ in out))

    def test_no_signal_uses_a_later_bar(self):
        full = CORE.signals(self.bars, loose())
        cut = 1800
        short = {k: (v[:cut] if isinstance(v, list) else v) for k, v in self.bars.items()}
        self.assertEqual(CORE.signals(short, loose()), full[:cut])


class TestCoreThroughThePipeline(unittest.TestCase):
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
            out = optimizer.run_pair(self.conn, "BTC-USD", "1h", 4, 77, family="core")
        self.assertIsNotNone(out)
        rows = self.conn.execute("SELECT family, params_json, dna FROM candidates").fetchall()
        self.assertEqual(len(rows), 4)
        for fam, pj, dna in rows:
            self.assertEqual(fam, "core")
            self.assertEqual(json.loads(pj)["family"], "core")
            self.assertEqual(json.loads(pj)["tick_size"], 0.001)    # the pair's, not 0.01
            self.assertEqual(dna, lineage.dna_of(json.loads(pj)))
        # A2's trial count and SR variance see only this family's trials
        self.assertEqual(V.sharpe_variance_from_candidates(self.conn, "BTC-USD", "1h", "core")[2], 4)
        self.assertEqual(V.sharpe_variance_from_candidates(self.conn, "BTC-USD", "1h")[2], 0)

    def test_ablation_and_sensitivity_use_core_lists(self):
        cfg = loose()
        vs = ablation.variants(cfg)
        self.assertEqual([v[0] for v in vs], ["use_shorts", "min_rel_vol", "max_dist_ema",
                                              "adx_threshold"])
        pruned, removed = ablation.prune(cfg, [{"name": n, "kind": k, "on_in_base": on,
                                                "verdict": "decoration"} for n, k, on, _ in vs])
        self.assertEqual(removed, ["min_rel_vol", "max_dist_ema", "adx_threshold"])
        self.assertTrue(pruned["use_shorts"])
        sens = V.param_sensitivity(self.conn, "BTC-USD", cfg, H, iso(T0 + 1500 * H),
                                   iso(T0 + (N_BARS - 1) * H), pct=(0.25,))
        self.assertEqual({r["param"] for r in sens["neighbours"]} - set(CORE.sensitivity_params),
                         set())
        for r in sens["neighbours"]:
            if r["param"] in CORE.int_params:
                self.assertIsInstance(r["to"], int)

    def test_lineage_register_records_family(self):
        optimizer.ensure_tables(self.conn)
        W.ensure_columns(self.conn)
        lineage.ensure_tables(self.conn)
        self.conn.execute("INSERT INTO candidates (id, pair, tf, params_json, passed, family)"
                          " VALUES (5, 'BTC-USD', '1h', ?, 1, 'core')",
                          (json.dumps(loose()),))
        sid = lineage.register(self.conn, 5)
        fam, dna = self.conn.execute("SELECT family, dna FROM strategies WHERE id=?",
                                     (sid,)).fetchone()
        self.assertEqual(fam, "core")
        self.assertEqual(dna, lineage.dna_of(loose()))

    def test_pine_export(self):
        script = pine_export.render(loose(), "SF test core", "note")
        self.assertNotIn("{", script.replace("{{", "").split("alert(")[0])
        for gone in ("vwap", "useTrailing", "avoidChop", "bigBars", "htfClose", "rsiV"):
            self.assertNotIn(gone, script)
        self.assertIn("adxThresh     = input.float(15.0,", script)
        self.assertIn("ta.dmi(adxLen, adxLen)", script)
        self.assertIn('"strategy":"SF test core"', script)


if __name__ == "__main__":
    unittest.main()
