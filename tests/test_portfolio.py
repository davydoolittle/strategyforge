"""
Tests for P6.1: app/portfolio.py, one candidate's cfg as a book across pairs
out of one capital pool. Synthetic candles; no forge.db needed.
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

import engine  # noqa: E402
import optimizer  # noqa: E402
import portfolio as P  # noqa: E402
import strategies  # noqa: E402
import validation as V  # noqa: E402

H = 3600
T0 = 1_600_000_000 // 86400 * 86400
N_BARS = 2500
CORE = strategies.get("core")
PAIRS = ["BTC-USD", "ETH-USD", "SOL-USD"]


def iso(t):
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def cfg():
    return {**CORE.default_cfg(), "adx_threshold": 15.0, "min_rel_vol": 0.9, "max_dist_ema": 3.0}


def book_db():
    """Three pairs of trending swings (different seeds and phases), one core
    candidate on BTC with pinned windows, plus a few same-family trials so A2
    has a trial count and an SR variance."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE candles (pair TEXT, ts INTEGER, open REAL, high REAL, low REAL,"
                 " close REAL, volume REAL, PRIMARY KEY (pair, ts))")
    for k, pair in enumerate(PAIRS):
        rng = random.Random(7 + k)
        px = 100.0
        for i in range(N_BARS):
            o = px
            r = 0.006 * math.sin(i / 120 + k) + rng.gauss(0, 0.007)
            c = px * (1 + r)
            vol = 100 * (1 + 3 * abs(r) / 0.007 * rng.random())
            conn.execute("INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (pair, T0 + i * H, o, max(o, c) * 1.002, min(o, c) * 0.998, c, vol))
            px = c
    optimizer.ensure_tables(conn)
    is_start, split, end = iso(T0 + 300 * H), iso(T0 + 1800 * H), iso(T0 + (N_BARS - 1) * H)
    conn.execute("INSERT INTO candidates (id, pair, tf, params_json, passed, family, is_start,"
                 " split_ts, oos_end, is_sharpe, is_trades) VALUES"
                 " (1, 'BTC-USD', '1h', ?, 1, 'core', ?, ?, ?, 0.1, 50)",
                 (json.dumps(cfg()), is_start, split, end))
    for i, sr in enumerate((0.05, -0.1, 0.2, 0.0)):
        conn.execute("INSERT INTO candidates (pair, tf, params_json, family, is_sharpe, is_trades)"
                     " VALUES ('BTC-USD', '1h', '{}', 'core', ?, 40)", (sr,))
    conn.commit()
    return conn


def patched():
    return (mock.patch.object(P.collector, "PAIRS", PAIRS),
            mock.patch.dict(P.collector.TICK_SIZE, {"BTC-USD": 0.01, "ETH-USD": 0.01},
                            clear=True))


class TestBookSizing(unittest.TestCase):
    def test_one_pair_book_is_the_backtest_to_the_cent(self):
        conn = book_db()
        self.addCleanup(conn.close)
        r = engine.backtest(conn, "BTC-USD", cfg(), iso(T0 + 300 * H), iso(T0 + (N_BARS - 1) * H))
        self.assertGreater(r["n_trades"], 5)
        b = P.book({"BTC-USD": r["trades"]}, 10000.0)
        self.assertEqual(len(b["trades"]), r["n_trades"])
        self.assertAlmostEqual(b["final_equity"] - 10000.0, r["net"], places=6)
        for bt, t in zip(b["trades"], r["trades"]):
            self.assertAlmostEqual(bt["pnl"], t["pnl"], places=6)
        self.assertEqual(b["max_open"], 1)

    def test_pool_sizing_and_event_order(self):
        # A wins 10% at a STOP/TARGET inside bar 100: B, entering at bar 100's
        # open, must be sized from 10,000 (A's exit comes later in that bar)
        a = {"dir": "long", "entry_ts": 0, "exit_ts": 100, "reason": "target", "pnl": 1000.0}
        b = {"dir": "long", "entry_ts": 100, "exit_ts": 200, "reason": "stop", "pnl": -100.0}
        out = P.book({"A": [a], "B": [b]}, 10000.0)
        by = {t["pair"]: t for t in out["trades"]}
        self.assertEqual(by["B"]["equity_at_entry"], 10000.0)
        self.assertAlmostEqual(by["B"]["pnl"], -100.0)
        self.assertEqual(out["max_open"], 2)
        # ...but an exit AT the open (reverse on opposite) is realised first
        a2 = {**a, "reason": "opposite"}
        out = P.book({"A": [a2], "B": [b]}, 10000.0)
        by = {t["pair"]: t for t in out["trades"]}
        self.assertEqual(by["B"]["equity_at_entry"], 11000.0)
        self.assertAlmostEqual(by["B"]["pnl"], -110.0)       # same 1% return, bigger pool
        self.assertEqual(out["max_open"], 1)

    def test_daily_returns_spread_pro_rata_over_realised_equity(self):
        d = 86400
        t = {"entry_ts": T0 + d // 2, "exit_ts": T0 + d + d // 2, "pnl": 200.0}
        rets = P.daily_returns([t], 10000.0, T0, T0 + 2 * d)
        self.assertEqual(len(rets), 3)
        self.assertAlmostEqual(rets[0], 100.0 / 10000.0)
        self.assertAlmostEqual(rets[1], 100.0 / 10000.0)      # P&L realised on day 1, not before
        self.assertAlmostEqual(rets[2], 0.0)

    def test_mean_pairwise_corr(self):
        s = [math.sin(i) for i in range(100)]
        m, rhos = P.mean_pairwise_corr({"A": (0, s), "B": (0, s), "C": (10, s[10:] + s[:10])})
        self.assertEqual(len(rhos), 3)
        self.assertAlmostEqual(dict(rhos)[("A", "B")], 1.0)
        # C is A shifted onto later days: aligned on the calendar it is A again
        self.assertAlmostEqual(dict(rhos)[("A", "C")], 1.0)


class TestEvaluate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = book_db()
        p1, p2 = patched()
        with p1, p2:
            cls.r = P.evaluate(cls.conn, 1)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_unverified_tick_is_excluded(self):
        self.assertEqual(self.r["pairs"], ["BTC-USD", "ETH-USD"])
        self.assertEqual(self.r["excluded_unverified_tick"], ["SOL-USD"])

    def test_home_leg_is_the_pinned_backtest(self):
        full = engine.backtest(self.conn, "BTC-USD", cfg(), iso(T0 + 300 * H),
                               iso(T0 + (N_BARS - 1) * H))
        leg = self.r["legs"]["BTC-USD"]["full"]
        self.assertEqual(leg["n_trades"], full["n_trades"])
        self.assertAlmostEqual(leg["net"], full["net"], places=6)

    def test_book_sr_per_trade_is_the_pooled_sr(self):
        p1, p2 = patched()
        with p1, p2:
            _, legs, _ = P.legs_for(self.conn, 1)
        pooled = [r for leg in legs.values()
                  for r in V.trade_returns(leg["full"], 10000.0)]
        b = self.r["book"]["full"]
        self.assertEqual(b["n_trades"], len(pooled))
        self.assertAlmostEqual(b["a2"]["sr_per_trade"], V.sharpe(sorted(pooled)), places=9)
        self.assertEqual(sum(b["trades_by_pair"].values()), b["n_trades"])

    def test_a2_uses_home_family_trials(self):
        a = self.r["book"]["full"]["a2"]
        self.assertEqual(a["n_trials"], 5)
        var, _, n, _ = V.sharpe_variance_from_candidates(self.conn, "BTC-USD", "1h", "core")
        self.assertEqual(a["sr_variance"], var)
        self.assertAlmostEqual(a["expected_max_sr"], V.expected_max_sharpe(n, var))
        self.assertEqual(a["passed"], a["dsr"] >= P.MIN_DSR)

    def test_nothing_written(self):
        before = self.conn.total_changes
        p1, p2 = patched()
        with p1, p2:
            P.evaluate(self.conn, 1)
        self.assertEqual(self.conn.total_changes, before)


if __name__ == "__main__":
    unittest.main()
