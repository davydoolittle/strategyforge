"""
Tests for P5.2: the strategy-family registry, family-aware DNA, and
engine.backtest dispatching signals to the cfg's family while keeping
execution (fills, stops, targets) its own.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import math
import os
import random
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import engine  # noqa: E402
import lineage  # noqa: E402
import optimizer  # noqa: E402
import pine_export  # noqa: E402
import strategies  # noqa: E402
from strategies.base import Strategy  # noqa: E402

H = 3600
T0 = 1_600_000_000 // H * H
N_BARS = 800


def candles_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
                 " close REAL, volume REAL, PRIMARY KEY (pair, ts))")
    rng = random.Random(3)
    px = 100.0
    for i in range(N_BARS):
        o = px
        c = px * (1 + 0.004 * math.sin(i / 40) + rng.gauss(0, 0.004))
        conn.execute("INSERT INTO candles VALUES ('BTC-USD', ?, ?, ?, ?, ?, 100)",
                     (T0 + i * H, o, max(o, c) * 1.003, min(o, c) * 0.997, c))
        px = c
    conn.commit()
    return conn


def iso(t):
    return engine.datetime.fromtimestamp(t, tz=engine.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


class EveryNth(Strategy):
    """Test family: long on every 50th bar, short on every 50th + 25."""
    name = "test_every_nth"
    dna_keys = ("period", "atr_len", "stop_atr", "rr_ratio", "risk_pct", "use_risk_sizing")

    def default_cfg(self):
        return {"family": self.name, "period": 50,
                "atr_len": 14, "stop_atr": 2.0, "rr_ratio": 2.0,
                "risk_pct": 1.0, "use_risk_sizing": True,
                "initial_capital": 10000.0, "commission_pct": 0.1,
                "slippage_ticks": 5, "tick_size": 0.01}

    def signals(self, bars, cfg):
        self.last_from_i = bars["from_i"]
        p = cfg["period"]
        return [(i % p == 0, i % p == p // 2) for i in range(len(bars["ts"]))]


class TestRegistry(unittest.TestCase):
    def test_v41_is_the_default_family(self):
        self.assertIn("smrt_v41", strategies.names())
        self.assertEqual(strategies.DEFAULT_FAMILY, "smrt_v41")
        self.assertEqual(strategies.family_of({"ema_fast": 9}), "smrt_v41")
        self.assertIs(strategies.for_cfg(engine.default_cfg()), strategies.get("smrt_v41"))

    def test_unknown_family_raises(self):
        with self.assertRaises(ValueError):
            strategies.get("nope")
        with self.assertRaises(ValueError):
            engine.backtest(None, "BTC-USD", {"family": "nope"}, "2020-01-01", "2020-01-02")

    def test_registering_a_name_twice_raises(self):
        with self.assertRaises(ValueError):
            strategies.register(type(strategies.get("smrt_v41")))

    def test_every_family_declares_its_cfg(self):
        for name in strategies.names():
            fam = strategies.get(name)
            cfg = fam.default_cfg()
            self.assertEqual(strategies.family_of(cfg), name)
            trade_keys = set(cfg) - strategies.NON_DNA_KEYS - {"family"}
            self.assertEqual(trade_keys, set(fam.dna_keys), name)
            for k in (set(fam.sensitivity_params) | set(fam.numeric_params)
                      | set(fam.toggle_params) | set(fam.toggles) | set(fam.traps)):
                self.assertIn(k, cfg, f"{name}: {k} listed but not in default_cfg")
            self.assertTrue(set(fam.prunable_toggles) <= set(fam.toggles))
            self.assertTrue(fam.cfg_ok(cfg))
            for _ in range(20):
                s = fam.sample_cfg(random.Random(_))
                self.assertEqual(set(s), set(cfg), name)
                self.assertTrue(fam.cfg_ok(s), name)


class TestDna(unittest.TestCase):
    def test_explicit_default_family_hashes_like_none(self):
        cfg = engine.default_cfg()
        self.assertEqual(lineage.dna_of(cfg), lineage.dna_of({**cfg, "family": "smrt_v41"}))

    def test_family_changes_dna(self):
        cfg = {"ema_fast": 9, "stop_atr": 2.0}
        self.assertNotEqual(lineage.dna_of(cfg), lineage.dna_of({**cfg, "family": "core"}))

    def test_optimizer_samples_through_the_family(self):
        # test_regression checks every stored seeded row against the sampler
        self.assertEqual(optimizer.sample_cfg(random.Random(5)),
                         strategies.get("smrt_v41").sample_cfg(random.Random(5)))


class TestEngineDispatch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if EveryNth.name not in strategies.REGISTRY:
            strategies.register(EveryNth)
        cls.fam = strategies.get(EveryNth.name)
        cls.conn = candles_db()

    @classmethod
    def tearDownClass(cls):
        strategies.REGISTRY.pop(EveryNth.name, None)
        cls.conn.close()

    def run_bt(self, cfg, start_bar=0):
        return engine.backtest(self.conn, "BTC-USD", cfg, iso(T0 + start_bar * H),
                               iso(T0 + (N_BARS - 1) * H))

    def test_family_signals_fill_at_next_open(self):
        r = self.run_bt(self.fam.default_cfg())
        self.assertGreater(r["n_trades"], 3)
        ents = {e["ts"] for e in r["entries"]}
        for e in r["entries"]:
            i = (e["ts"] - T0) // H
            self.assertTrue(i % 50 in (0, 25), i)
            self.assertGreaterEqual(i, engine.WARMUP_BARS)
        for t in r["trades"]:
            self.assertIn(t["signal_ts"], ents)
            self.assertEqual(t["entry_ts"], t["signal_ts"] + H)

    def test_from_i_is_first_bar_in_window(self):
        self.run_bt(self.fam.default_cfg(), start_bar=450)
        self.assertEqual(self.fam.last_from_i, 450)
        self.run_bt(self.fam.default_cfg(), start_bar=0)
        self.assertEqual(self.fam.last_from_i, engine.WARMUP_BARS)

    def test_no_trailing_keys_needed(self):
        cfg = self.fam.default_cfg()
        self.assertNotIn("use_trailing", cfg)
        reasons = {t["reason"] for t in self.run_bt(cfg)["trades"]}
        self.assertTrue(reasons <= {"stop", "target", "opposite"}, reasons)

    def test_forced_signals_bypass_the_family(self):
        sig = {T0 + 400 * H: "long"}
        r = engine.backtest(self.conn, "BTC-USD", self.fam.default_cfg(), iso(T0),
                            iso(T0 + (N_BARS - 1) * H), signals=sig)
        self.assertEqual([e["ts"] for e in r["entries"]], [T0 + 400 * H])


class TestV41Signals(unittest.TestCase):
    def test_signals_shape_and_unevaluated_prefix(self):
        conn = candles_db()
        self.addCleanup(conn.close)
        ts, o, h, l, c, v = engine.load_candles(conn, "BTC-USD", T0 + N_BARS * H)
        fam = strategies.get("smrt_v41")
        cfg = fam.default_cfg()
        bars = {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v,
                "atr": engine.atr(h, l, c, cfg["atr_len"]), "from_i": 500}
        out = fam.signals(bars, cfg)
        self.assertEqual(len(out), len(ts))
        self.assertTrue(all(x == (False, False) for x in out[:500]))

    def test_4h_uses_daily_htf(self):
        fam = strategies.get("smrt_v41")
        self.assertEqual(fam.for_timeframe(fam.default_cfg(), 4 * H)["htf_seconds"], 86400)
        self.assertEqual(fam.for_timeframe(fam.default_cfg(), H)["htf_seconds"], 4 * H)

    def test_pine_template_fills(self):
        script = pine_export.render(engine.default_cfg(), "T", "note")
        self.assertIn('htfTF         = input.timeframe("240"', script)
        self.assertIn("useLongs      = input.bool(true,", script)
        self.assertNotIn("{use_", script)


if __name__ == "__main__":
    unittest.main()
