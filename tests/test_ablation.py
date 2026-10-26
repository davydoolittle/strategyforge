"""
Tests for ablation.py (P2.5). Classification and pruning are tested directly;
the end-to-end path uses a fake engine.backtest so the expected verdicts are
known by construction.
Run from the repo root:   python -m unittest discover -s tests -v
"""

import json
import os
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import ablation as A  # noqa: E402
import engine  # noqa: E402
import optimizer  # noqa: E402
import windows as W  # noqa: E402


def trade(i, pnl, shift=0):
    return {"entry_ts": i * 10 + shift, "exit_ts": i * 10 + 5 + shift,
            "dir": "long", "pnl": pnl}


def summ(is_pnls, oos_pnls, shift=0):
    return A.summarize([trade(i, p, shift) for i, p in enumerate(is_pnls)],
                       [trade(100 + i, p, shift) for i, p in enumerate(oos_pnls)],
                       10_000.0, 16, 0.1)


BASE = summ([100, -50, 100, -50], [100, -50])            # PF 2.0 / 2.0


class TestVariants(unittest.TestCase):
    def test_fourteen_one_change_variants(self):
        cfg = engine.default_cfg()
        vs = A.variants(cfg)
        self.assertEqual(len(vs), 14)
        self.assertEqual([v[0] for v in vs], A.TOGGLES + list(A.TRAPS))
        for name, _, _, c2 in vs:
            diff = [k for k in cfg if cfg[k] != c2[k]]
            self.assertEqual(diff, [name])

    def test_trap_inactive_when_its_toggle_is_off(self):
        cfg = {**engine.default_cfg(), "use_volume": False}
        on = {v[0]: v[2] for v in A.variants(cfg)}
        self.assertFalse(on["min_rel_vol"])
        self.assertTrue(on["min_ema_sep"])          # ungated


class TestClassify(unittest.TestCase):
    def test_identical_trades_are_inactive(self):
        self.assertEqual(A.classify(BASE, summ([100, -50, 100, -50], [100, -50]), True),
                         "inactive")

    def test_worse_without_on_both_windows_earns(self):
        without = summ([100, -50, 100, -50, -100], [100, -50, -50])
        self.assertEqual(A.classify(BASE, without, True), "earns")

    def test_better_without_on_both_windows_hurts(self):
        without = summ([100, -50, 100, -50, 100], [100, -50, 100])
        self.assertEqual(A.classify(BASE, without, True), "hurts")

    def test_windows_disagree_is_decoration(self):
        without = summ([100, -50, 100, -50, 100], [100, -50, -50])   # IS up, OOS down
        self.assertEqual(A.classify(BASE, without, True), "decoration")

    def test_same_pf_different_trades_is_decoration(self):
        self.assertEqual(A.classify(BASE, summ([100, -50, 100, -50], [100, -50], shift=1), True),
                         "decoration")

    def test_off_feature_is_judged_by_adding_it(self):
        # base lacks the feature; the variant (with it) is worse -> it hurts
        with_ = summ([100, -50, 100, -50, -100], [100, -50, -50])
        self.assertEqual(A.classify(BASE, with_, False), "hurts")

    def test_small_gap_is_decoration(self):
        without = summ([100, -50, 100, -51], [100, -51], shift=1)  # PF 1.98 / 1.96
        self.assertEqual(A.classify(BASE, without, True), "decoration")


class TestPrune(unittest.TestCase):
    def rows(self, **verdicts):
        cfg = engine.default_cfg()
        return cfg, [{"name": n, "kind": k, "on_in_base": on,
                      "verdict": verdicts.get(n, "earns")}
                     for n, k, on, _ in A.variants(cfg)]

    def test_only_active_non_earning_features_switched_off(self):
        cfg, rows = self.rows(use_vwap="decoration", max_dist_ema="hurts",
                              use_trailing="decoration", use_shorts="decoration")
        pruned, removed = A.prune(cfg, rows)
        self.assertEqual(removed, ["use_vwap", "max_dist_ema"])
        self.assertFalse(pruned["use_vwap"])
        self.assertEqual(pruned["max_dist_ema"], A.TRAPS["max_dist_ema"][0])
        self.assertTrue(pruned["use_shorts"])       # a direction, never pruned
        self.assertFalse(pruned["use_trailing"])    # was off already: untouched
        self.assertEqual(A.active_toggles(pruned), A.active_toggles(cfg) - 1)

    def test_nothing_to_prune(self):
        cfg, rows = self.rows()
        self.assertEqual(A.prune(cfg, rows)[1], [])


def fake_backtest(conn, pair, cfg, start_iso, end_iso, tf_seconds=3600):
    """Volume filter carries the result; trailing changes the trades but not
    the PF (decoration); avoid_chop never fires (inactive)."""
    off = W._ts(start_iso)
    pnls = [100, -50, 100, -50]
    if not cfg["use_volume"]:
        pnls += [-100, -100]
    shift = 1 if cfg["use_trailing"] else 0
    trades = [{"entry_ts": off + i * 10 + shift, "exit_ts": off + i * 10 + 5 + shift,
               "dir": "long", "pnl": p} for i, p in enumerate(pnls)]
    gp = sum(p for p in pnls if p > 0)
    gl = -sum(p for p in pnls if p <= 0)
    return {"trades": trades, "n_trades": len(trades), "net": sum(pnls),
            "pf": gp / gl, "max_dd": 50.0, "win_rate": 50.0}


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        optimizer.ensure_tables(self.conn)
        A.ensure_tables(self.conn)
        cfg = {**engine.default_cfg(), "use_vwap": False}
        self.conn.execute(
            "INSERT INTO candidates (run_at, pair, tf, params_json, passed, is_start,"
            " split_ts, oos_end, windows_backfilled, dna) VALUES"
            " ('2026-10-26', 'BTC-USD', '1h', ?, 1, '2024-01-01T00:00:00',"
            " '2025-01-01T00:00:00', '2025-06-01T00:00:00', 0, 'x')", (json.dumps(cfg),))
        p = mock.patch.object(A.engine, "backtest", fake_backtest)
        p.start()
        self.addCleanup(p.stop)

    def test_verdicts_prune_and_register(self):
        rep, raw = A.ablate(self.conn, 1, verbose=False)
        v = {r["name"]: r["verdict"] for r in rep["rows"]}
        self.assertEqual(v["use_volume"], "earns")
        self.assertEqual(v["use_trailing"], "decoration")    # P2.8
        self.assertEqual(v["avoid_chop"], "inactive")
        self.assertEqual(v["use_vwap"], "inactive")          # off, adding it changes nothing
        self.assertNotIn("use_volume", rep["pruned"]["removed"])
        self.assertIn("avoid_chop", rep["pruned"]["removed"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ablation_reports").fetchone()[0], 1)

        new_id, created = A.register(self.conn, 1, rep, raw)
        self.assertTrue(created)
        row = self.conn.execute("SELECT ablation_of, is_start, oos_end, is_trades, passed, params_json"
                                " FROM candidates WHERE id=?", (new_id,)).fetchone()
        self.assertEqual(row[:4], (1, "2024-01-01T00:00:00", "2025-06-01T00:00:00", 4))
        self.assertEqual(row[4], 0)                          # 4 IS trades < IS_MIN_TRADES
        self.assertNotIn("history_start_ts", json.loads(row[5]))
        self.assertEqual(A.register(self.conn, 1, rep, raw), (new_id, False))


if __name__ == "__main__":
    unittest.main()
